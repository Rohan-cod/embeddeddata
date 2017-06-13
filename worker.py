#! /usr/bin/env python
# -*- coding: UTF-8 -*-
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General License for more details.
#
# You should have received a copy of the GNU General License
# along with self program.  If not, see <http://www.gnu.org/licenses/>
#

import datetime
import json
import os
import shutil
import tempfile
import threading
import traceback
import uuid

import pywikibot
from pywikibot.data.api import APIError
from pywikibot.throttle import Throttle
from redis import Redis

from config import REDIS_KEY
from detection import detect
from detection.by_ending import ARCHIVE_TYPES, UNKNOWN_TYPES


MESSAGE_PREFIX = ('This file contains [[COM:CSD#F9|'
                  'embedded data]]: ')


def sizeof_fmt(num, suffix='B'):
    # Source: http://stackoverflow.com/a/1094933
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def run_worker():
    try:
        tmpdir = tempfile.mkdtemp()

        site = pywikibot.Site(user="Embedded Data Bot")
        site._throttle = Throttle(site, multiplydelay=False)

        # Multi-workers are enough to cause problems, no need for internal
        # locking to cause even more problems
        site.lock_page = lambda *args, **kwargs: None  # noop
        site.unlock_page = lambda *args, **kwargs: None  # noop

        redis = Redis(host="tools-redis")

        while True:
            _, change = redis.blpop(REDIS_KEY)
            change = json.loads(change)
            filepage = pywikibot.FilePage(site, change['title'])

            if not filepage.exists():
                continue

            for i in range(8):
                try:
                    filepage.get_file_history()
                except pywikibot.exceptions.PageRelatedError as e:
                    # pywikibot.exceptions.PageRelatedError:
                    # loadimageinfo: Query on ... returned no imageinfo
                    pywikibot.exception(e)
                    site.throttle(write=True)
                else:
                    break
            else:
                raise

            try:
                revision = filepage.get_file_history()[
                    pywikibot.Timestamp.fromtimestampformat(
                        change['log_params']['img_timestamp'])]
            except KeyError:
                try:
                    revision = filepage.get_file_history()[
                        pywikibot.Timestamp.fromtimestamp(
                            change['timestamp'])]
                except KeyError:
                    revision = filepage.latest_file_info
                    pywikibot.warning(
                        'Cannot fetch specified revision, falling back to '
                        'latest revision.')

            if pywikibot.User(site, revision.user).editCount(
                    force=True) > 200:
                continue

            pywikibot.output('Working on: %s at %s' % (change['title'],
                                                       revision.timestamp))

            path = os.path.join(tmpdir, str(uuid.uuid1()))

            # Download
            try:
                for i in range(8):
                    try:
                        success = filepage.download(path, revision=revision)
                    except Exception as e:
                        pywikibot.exception(e)
                        success = False
                    if success:
                        break
                    else:
                        pywikibot.warning(
                            'Possibly corrupted download on attempt %d' % i)
                        site.throttle(write=True)
                else:
                    pywikibot.warning('FIXME: Download attempt exhausted')

                res = detect(path)
                if res:
                    msg = []
                    for item in res:
                        pos = '%s (%s bytes, via %s)' % (
                            sizeof_fmt(item['pos']),
                            item['pos'],
                            ','.join(item['via']))
                        if not item['posexact']:
                            pos = 'about ' + pos

                        if item['mime'][0] in UNKNOWN_TYPES:
                            mime = 'Unidentified type (%s, %s)' % item['mime']
                        else:
                            mime = 'Identified type: %s (%s)' % item['mime']
                        msg.append('After %s: %s' % (pos, mime))
                    msg = '; '.join(msg)

                    pywikibot.output(u"\n\n>>> %s <<<"
                                     % filepage.title(asLink=True))
                    pywikibot.output(msg)

                    execute_file(filepage, revision, msg, res, path)

            except Exception:
                traceback.print_exc()
            finally:
                os.remove(path)

        pywikibot.output("Exit - THIS SHOULD NOT HAPPEN")
    finally:
        shutil.rmtree(tmpdir)


def execute_file(filepage, revision, msg, res, path):
    if all([item['posexact'] and
            item['mime'][0] == filepage.latest_file_info.mime
            for item in res]):
        overwrite(filepage, msg, res, path)
        return

    if any([item['posexact'] and item['mime'][0] in ARCHIVE_TYPES
            for item in res]):
        if len(filepage.get_file_history()) == 1 or \
                csd_g7_eligible(filepage, revision):
            protect(filepage, msg)
            delete(filepage, msg)
            protect(filepage, msg)
            for i in range(8):
                threading.Timer((i+1)*8, delete, (filepage, msg)).start()
        else:
            overwrite(filepage, msg, res, path)
            try:
                revdel(filepage, revision, msg)
            except Exception:
                traceback.print_exc()
                add_speedy(filepage, msg)
        return

    add_speedy(filepage, msg)


def csd_g7_eligible(filepage, revision):
    # https://commons.wikimedia.org/w/index.php?title=Commons:Criteria_for_speedy_deletion&oldid=244664065#G7
    # == General reasons ==
    # 7. Author or uploader request deletion
    # Original author or uploader requests deletion of recently created
    # (<7 days) unused content. For author/uploader requests for deletion of
    # content that is older a deletion request should be filled instead.

    # condition: recently created
    if (pywikibot.Timestamp.now() - filepage.oldest_file_info.timestamp
            >= datetime.timedelta(days=7)):
        return False

    # condition: author request
    assert_username = [revision.user, filepage.site.username()]
    for rev in filepage.get_file_history():
        if rev.user not in assert_username:
            return False

    # condition: unused
    req = filepage.site._simple_request(
        action='query',
        prop='globalusage',
        titles=filepage.title()
    )
    try:
        res = req.submit()
    except Exception as e:
        pywikibot.exception(e)
        return False
    else:
        p = res['query']['pages']
        gu = p[p.keys()[0]]['globalusage']
        if gu:
            return False

    return True


def retry_apierror(f):
    for i in range(8):
        try:
            f()
        except APIError:
            pywikibot.warning(
                'Failed API request on attempt %d' % i)
        else:
            break
    else:
        raise


def overwrite(filepage, msg, res, path):
    filepage._file_revisions.clear()

    if not filepage.get_file_history():
        pywikibot.warning("Page doesn't exist, skipping upload.")
        return

    with tempfile.NamedTemporaryFile() as tmp:
        with open(path, 'rb') as old:
            shutil.copyfileobj(old, tmp)

        tmp.truncate(res[0]['pos'])
        retry_apierror(
            lambda:
            filepage.upload(tmp.name,
                            comment=MESSAGE_PREFIX+msg,
                            ignore_warnings=True)
        )


def delete(filepage, msg):
    for i in range(8):
        filepage._file_revisions.clear()
        filepage.clear_cache()

        try:
            hist = filepage.get_file_history()
        except Exception:
            hist = None

        if not filepage.exists() and not hist:
            break
        else:
            if i:
                pywikibot.warning(
                    'File exist still before deletion on attempt %d' % i)
            pywikibot.output('Executing delete on %s' % filepage)

            retry_apierror(
                lambda:
                filepage.delete(MESSAGE_PREFIX+msg, prompt=False)
            )
    else:
        pywikibot.warning('FIXME: Deletion attempt exhausted')


def protect(filepage, msg):
    # Make sure this is not executed on a page that is already protected.
    # For newly uploaded files this is fine.
    protectmsg = (
        '[[Commons:Protection policy|Protection against re-creation]]: ' +
        MESSAGE_PREFIX + msg)
    ev = threading.Event()

    def _protect(typ):
        try:
            filepage.protect(
                protections={typ: 'autoconfirmed'},
                expiry='1 minute',
                reason=protectmsg,
                prompt=False
            )
        except (APIError, pywikibot.Error):
            pass
        else:
            ev.set()

    upload_thread = threading.Thread(target=lambda: _protect('upload'))
    create_thread = threading.Thread(target=lambda: _protect('create'))
    upload_thread.start()
    create_thread.start()
    upload_thread.join()
    create_thread.join()

    if not ev.is_set():
        pywikibot.warning('Protection of %s failed.' % filepage)


def revdel(filepage, revision, msg):
    assert filepage.get_file_history()[revision.timestamp]

    for i in range(8):
        try:
            filepage._file_revisions.clear()
            revision = filepage.get_file_history()[revision.timestamp]
            assert revision.archivename and '!' in revision.archivename
        except (KeyError, AssertionError):
            pywikibot.warning(
                'Failed to load new revision history on attempt %d' % i)
            filepage.site.throttle(write=True)
        else:
            break
    else:
        raise

    revid = revision.archivename.split('!')[0]
    retry_apierror(
        lambda:
        filepage.site._simple_request(
            action='revisiondelete',
            type='oldimage',
            target=filepage.title(),
            ids=revid,
            hide='content',
            reason=MESSAGE_PREFIX+msg,
            token=filepage.site.tokens['csrf']
        ).submit()
    )


def add_speedy(filepage, msg):
    filepage.clear_cache()

    if not filepage.exists():
        pywikibot.warning("Page doesn't exist, skipping save.")
        return

    # Make sure no edit conflicts happen here
    retry_apierror(
        lambda:
        filepage.save(prependtext='{{embedded data|suspect=1|1=%s}}\n' % msg,
                      summary='Bot: Adding {{[[Template:Embedded data|'
                      'embedded data]]}} to this embedded data suspect.')
    )


def main():
    pywikibot.handleArgs()
    run_worker()


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()
