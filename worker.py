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

import json
import os
import shutil
import tempfile
import time
import traceback
import urllib
import uuid

import pywikibot
from pywikibot.data.api import APIError
from redis import Redis

from config import REDIS_KEY
from detection import detect
from detection.by_ending import ARCHIVE_TYPES, UNKNOWN_TYPES


def sizeof_fmt(num, suffix='B'):
    # Source: http://stackoverflow.com/a/1094933
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def throttle():
    TIME = 1
    pywikibot.output('Throttle {} seconds'.format(TIME))
    time.sleep(TIME)


# Forked from pywikibot due to T166939
def download(page, filename=None, chunk_size=100 * 1024, revision=None):
    from pywikibot.comms import http
    from pywikibot.tools import compute_file_hash

    if filename is None:
        filename = page.title(as_filename=True, withNamespace=False)

    filename = os.path.expanduser(filename)

    if revision is None:
        revision = page.latest_file_info

    req = http.fetch(revision.url, stream=True)
    if req.status == 200:
        try:
            with open(filename, 'wb') as f:
                for chunk in req.data.iter_content(chunk_size):
                    f.write(chunk)
        except IOError as e:
            raise e

        sha1 = compute_file_hash(filename)
        return sha1 == revision.sha1
    else:
        pywikibot.warning('Unsuccesfull request (%s): %s' % (req.status, req.uri))
        return False


def run_worker():
    try:
        tmpdir = tempfile.mkdtemp()

        site = pywikibot.Site(user="Embedded Data Bot")
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
                    throttle()
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
                        # TODO: make sure doenloaded file is of `revision`
                        # success = filepage.download(path)
                        success = download(
                            filepage, filename=path, revision=revision)
                    except Exception as e:
                        pywikibot.exception(e)
                        success = False
                    if success:
                        break
                    else:
                        pywikibot.warning(
                            'Possibly corrupted download on attempt %d' % i)
                        throttle()
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

                    msgprefix = ('This file contains [[COM:CSD#F9|'
                                 'embedded data]]: ')

                    pywikibot.output(u"\n\n>>> %s <<<"
                                     % filepage.title(asLink=True))
                    pywikibot.output(msg)

                    execute_file(filepage, revision, msg, msgprefix, res)

            except Exception:
                traceback.print_exc()
            finally:
                os.remove(path)

        pywikibot.output("Exit - THIS SHOULD NOT HAPPEN")
    finally:
        shutil.rmtree(tmpdir)


def execute_file(filepage, revision, msg, msgprefix, res):
    if all([item['posexact'] and
            item['mime'][0] == filepage.latest_file_info.mime
            for item in res]):
        overwrite(filepage, revision, msg, msgprefix, res)
        return

    if any([item['posexact'] and item['mime'][0] in ARCHIVE_TYPES
            for item in res]):
        if len(filepage.get_file_history()) == 1:
            add_speedy(filepage, revision, msg, msgprefix, res)
            delete(filepage, revision, msg, msgprefix, res)
        else:
            overwrite(filepage, revision, msg, msgprefix, res)
            try:
                revdel(filepage, revision, msg, msgprefix, res)
            except Exception:
                traceback.print_exc()
                add_speedy(filepage, revision, msg, msgprefix, res)
        return

    add_speedy(filepage, revision, msg, msgprefix, res)


def retry_apierror(f):
    for i in range(8):
        try:
            f()
        except APIError as e:
            if e.code != 'internal_api_error_LocalFileLockError':
                raise
            pywikibot.warning(
                'Failed API request on attempt %d' % i)
        else:
            break
    else:
        raise


def overwrite(filepage, revision, msg, msgprefix, res):
    with tempfile.NamedTemporaryFile() as tmp:
        urllib.urlretrieve(filepage.fileUrl(), tmp.name)
        tmp.truncate(res[0]['pos'])
        retry_apierror(
            lambda:
            filepage.upload(tmp.name,
                            comment=msgprefix+msg,
                            ignore_warnings=True)
        )


def delete(filepage, revision, msg, msgprefix, res):
    retry_apierror(
        lambda:
        filepage.delete(msgprefix+msg, prompt=False)
    )


def revdel(filepage, revision, msg, msgprefix, res):
    assert filepage.get_file_history()[revision.timestamp]

    for i in range(8):
        try:
            filepage._file_revisions.clear()
            revision = filepage.get_file_history()[revision.timestamp]
            assert revision.archivename and '!' in revision.archivename
        except (KeyError, AssertionError):
            pywikibot.warning(
                'Failed to load new revision history on attempt %d' % i)
            throttle()
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
            reason=msgprefix+msg,
            token=filepage.site.tokens['csrf']
        ).submit()
    )


def add_speedy(filepage, revision, msg, msgprefix, res):
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
