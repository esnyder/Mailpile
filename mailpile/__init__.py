#!/usr/bin/python
ABOUT="""\
Mailpile.py          a tool               Copyright 2011, Bjarni R. Einarsson
               for searching and                      <http://bre.klaki.net/>
           organizing piles of e-mail

This program is free software: you can redistribute it and/or modify it under
the terms of the  GNU  Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.
"""
###############################################################################
import cgi, codecs, datetime, email.parser, getopt, hashlib, locale, mailbox
import os, cPickle, random, re, rfc822, socket, struct, subprocess, sys
import tempfile, threading, time
import SocketServer
from SimpleXMLRPCServer import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from urlparse import parse_qs, urlparse
import lxml.html


global APPEND_FD_CACHE, APPEND_FD_CACHE_ORDER, APPEND_FD_CACHE_SIZE
global WORD_REGEXP, STOPLIST, BORING_HEADERS, DEFAULT_PORT, QUITTING

QUITTING = False

DEFAULT_PORT = 33411

WORD_REGEXP = re.compile('[^\s!@#$%^&*\(\)_+=\{\}\[\]:\"|;\'\\\<\>\?,\.\/\-]{2,}')

STOPLIST = set(['an', 'and', 'are', 'as', 'at', 'by', 'for', 'from',
                'has', 'http', 'in', 'is', 'it', 'mailto', 'og', 'or',
                're', 'so', 'the', 'to', 'was'])

BORING_HEADERS = ('received', 'date',
                  'content-type', 'content-disposition', 'mime-version',
                  'dkim-signature', 'domainkey-signature', 'received-spf')


class WorkerError(Exception):
  pass

class UsageError(Exception):
  pass

class AccessError(Exception):
  pass


def b64c(b): return b.replace('\n', '').replace('=', '').replace('/', '_')
def b64w(b): return b64c(b).replace('+', '-')

def sha1b64(s):
  h = hashlib.sha1()
  h.update(s.encode('utf-8'))
  return h.digest().encode('base64')

def strhash(s, length):
  s2 = re.sub('[^0123456789abcdefghijklmnopqrstuvwxyz]+', '',
              s.lower())[:(length-4)]
  while len(s2) < length:
    s2 += b64c(sha1b64(s)).lower()
  return s2[:length]

def b36(number):
  alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  base36 = ''
  while number:
    number, i = divmod(number, 36)
    base36 = alphabet[i] + base36
  return base36 or alphabet[0]

GPG_BEGIN_MESSAGE = '-----BEGIN PGP MESSAGE'
GPG_END_MESSAGE = '-----END PGP MESSAGE'
def decrypt_gpg(lines, fd):
  for line in fd:
    lines.append(line)
    if line.startswith(GPG_END_MESSAGE):
      break

  gpg = subprocess.Popen(['gpg', '--batch'],
                         stdin=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         stdout=subprocess.PIPE)
  lines = gpg.communicate(input=''.join(lines))[0].splitlines(True)
  if gpg.wait() != 0:
    raise AccessError("GPG was unable to decrypt the data.")

  return  lines

def gpg_open(filename, recipient, mode):
  fd = open(filename, mode)
  if recipient and ('a' in mode or 'w' in mode):
    gpg = subprocess.Popen(['gpg', '--batch', '-aer', recipient],
                           stdin=subprocess.PIPE,
                           stdout=fd)
    return gpg.stdin
  return fd


# Indexing messages is an append-heavy operation, and some files are
# appended to much more often than others.  This implements a simple
# LRU cache of file descriptors we are appending to.
APPEND_FD_CACHE = {}
APPEND_FD_CACHE_SIZE = 500
APPEND_FD_CACHE_ORDER = []
def flush_append_cache(ratio=1, count=None):
  drop = count or int(ratio*len(APPEND_FD_CACHE_ORDER))
  for fn in APPEND_FD_CACHE_ORDER[:drop]:
    APPEND_FD_CACHE[fn].close()
    del APPEND_FD_CACHE[fn]
  APPEND_FD_CACHE_ORDER[:drop] = []

def cached_open(filename, mode):
  # FIXME: This is not thread safe at all!
  if mode == 'a':
    if filename not in APPEND_FD_CACHE:
      if len(APPEND_FD_CACHE) > APPEND_FD_CACHE_SIZE:
        flush_append_cache(count=1)
      try:
        APPEND_FD_CACHE[filename] = open(filename, 'a')
      except (IOError, OSError):
        # Too many open files?  Close a bunch and try again.
        flush_append_cache(ratio=0.3)
        APPEND_FD_CACHE[filename] = open(filename, 'a')
      APPEND_FD_CACHE_ORDER.append(filename)
    else:
      APPEND_FD_CACHE_ORDER.remove(filename)
      APPEND_FD_CACHE_ORDER.append(filename)
    return APPEND_FD_CACHE[filename]
  else:
    if filename in APPEND_FD_CACHE:
      if 'w' in mode or mode == 'r+':
        APPEND_FD_CACHE[filename].close()
        del APPEND_FD_CACHE[filename]
        try:
          APPEND_FD_CACHE_ORDER.remove(filename)
        except ValueError:
          pass
      else:
        APPEND_FD_CACHE[filename].flush()
    return open(filename, mode)


##[ Enhanced mailbox and message classes ]#####################################

## Dear hackers!
##
## It would be great to have more mailbox classes.  They should be derived
## from or implement the same interfaces as Python's native mailboxes, with
## the additional constraint that they support pickling and unpickling using
## cPickle.  The mailbox class is also responsible for generating and parsing
## a "pointer" which should be a short as possible while still encoding the
## info required to locate this message and this message only within the
## larger mailbox.

class IncrementalMbox(mailbox.mbox):
  """A mbox class that supports pickling and a few mailpile specifics."""

  last_parsed = 0
  save_to = None

  def __getstate__(self):
    odict = self.__dict__.copy()
    # Pickle can't handle file objects.
    del odict['_file']
    return odict

  def __setstate__(self, dict):
    self.__dict__.update(dict)
    try:
      self._file = open(self._path, 'rb+')
    except IOError, e:
      if e.errno == errno.ENOENT:
        raise NoSuchMailboxError(self._path)
      elif e.errno == errno.EACCES:
        self._file = open(self._path, 'rb')
      else:
        raise
    self.update_toc()

  def update_toc(self):
    # FIXME: Does this break on zero-length mailboxes?

    # Scan for incomplete entries in the toc, so they can get fixed.
    for i in sorted(self._toc.keys()):
      if i > 0 and self._toc[i][0] is None:
        self._file_length = self._toc[i-1][0]
        self._next_key = i-1
        del self._toc[i-1]
        del self._toc[i]
        break
      elif self._toc[i][0] and not self._toc[i][1]:
        self._file_length = self._toc[i][0]
        self._next_key = i
        del self._toc[i]
        break

    self._file.seek(0, 2)
    if self._file_length == self._file.tell(): return

    self._file.seek(self._toc[self._next_key-1][0])
    line = self._file.readline()
    if not line.startswith('From '):
      raise IOError("Mailbox has been modified")

    self._file.seek(self._file_length-len(os.linesep))
    start = None
    while True:
      line_pos = self._file.tell()
      line = self._file.readline()
      if line.startswith('From '):
        if start:
          self._toc[self._next_key] = (start, line_pos - len(os.linesep))
          self._next_key += 1
        start = line_pos
      elif line == '':
        self._toc[self._next_key] = (start, line_pos)
        self._next_key += 1
        break
    self._file_length = self._file.tell()
    self.save(None)

  def save(self, session=None, to=None):
    if to:
      self.save_to = to
    if self.save_to and len(self) > 0:
      if session: session.ui.mark('Saving state to %s' % self.save_to)
      fd = open(self.save_to, 'w')
      cPickle.dump(self, fd)
      fd.close()

  def get_msg_size(self, toc_id):
    return self._toc[toc_id][1] - self._toc[toc_id][0]

  def get_msg_ptr(self, idx, toc_id):
    return '%s%s:%s' % (idx,
                        b36(self._toc[toc_id][0]),
                        b36(self.get_msg_size(toc_id)))

  def get_file_by_ptr(self, msg_ptr):
    start, length = msg_ptr[3:].split(':')
    start = int(start, 36)
    length = int(length, 36)
    return mailbox._PartialFile(self._file, start, start+length)


class Email(object):
  """This is a lazy-loading object representing a single email."""

  def __init__(self, idx, msg_idx):
    self.index = idx
    self.config = idx.config
    self.msg_idx = msg_idx
    self.msg_info = None
    self.msg_parsed = None

  def get_msg_info(self, field):
    if not self.msg_info:
      self.msg_info = self.index.get_msg_by_idx(self.msg_idx)
    return self.msg_info[field]

  def get_file(self):
    for msg_ptr in self.get_msg_info(self.index.MSG_PTRS).split(','):
      try:
        mbox = self.config.open_mailbox(None, msg_ptr[:3])
        return mbox.get_file_by_ptr(msg_ptr)
      except (IOError, OSError):
        pass
    return None

  def get_msg(self):
    if not self.msg_parsed:
      fd = self.get_file()
      if fd:
        self.msg_parsed = email.parser.Parser().parse(fd)
    if not self.msg_parsed:
      IndexError('Message not found?')
    return self.msg_parsed

  def is_thread(self):
    return (0 < len(self.get_msg_info(self.index.MSG_REPLIES)))

  def get(self, field, default=None):
    """Get one (or all) indexed fields for this mail."""
    field = field.lower()
    if field == 'subject':
      return self.get_msg_info(self.index.MSG_SUBJECT)
    elif field == 'from':
      return self.get_msg_info(self.index.MSG_FROM)
    else:
      return self.get_msg().get(field, default)

  def get_body_text(self):
    for part in self.get_msg().walk():
      charset = part.get_charset() or 'iso-8859-1'
      if part.get_content_type() == 'text/plain':
        return part.get_payload(None, True).decode(charset)
    return ''


##[ The search and index code itself ]#########################################

class PostingList(object):
  """A posting list is a map of search terms to message IDs."""

  MAX_SIZE = 60  # perftest gives: 75% below 500ms, 50% below 100ms
  HASH_LEN = 24

  @classmethod
  def Optimize(cls, session, idx, force=False):
    flush_append_cache()

    postinglist_kb = session.config.get('postinglist_kb', cls.MAX_SIZE)
    postinglist_dir = session.config.postinglist_dir()

    # Pass 1: Compact all files that are 90% or more of our target size
    for fn in sorted(os.listdir(postinglist_dir)):
      if QUITTING: break
      if (force
      or  os.path.getsize(os.path.join(postinglist_dir, fn)) >
                                                        900*postinglist_kb):
        session.ui.mark('Pass 1: Compacting >%s<' % fn)
        # FIXME: Remove invalid and deleted messages from posting lists.
        cls(session, fn, sig=fn).save()

    # Pass 2: While mergable pair exists: merge them!
    flush_append_cache()
    files = [n for n in os.listdir(postinglist_dir) if len(n) > 1]
    files.sort(key=lambda a: -len(a))
    for fn in files:
      if QUITTING: break
      size = os.path.getsize(os.path.join(postinglist_dir, fn))
      fnp = fn[:-1]
      while not os.path.exists(os.path.join(postinglist_dir, fnp)):
        fnp = fnp[:-1]
      size += os.path.getsize(os.path.join(postinglist_dir, fnp))
      if (size < (1024*postinglist_kb-(cls.HASH_LEN*6))):
        session.ui.mark('Pass 2: Merging %s into %s' % (fn, fnp))
        fd = cached_open(os.path.join(postinglist_dir, fn), 'r')
        fdp = cached_open(os.path.join(postinglist_dir, fnp), 'a')
        try:
          for line in fd:
            fdp.write(line)
        except:
          flush_append_cache()
          raise
        finally:
          fd.close()
          os.remove(os.path.join(postinglist_dir, fn))

    flush_append_cache()
    filecount = len(os.listdir(postinglist_dir))
    session.ui.mark('Optimized %s posting lists' % filecount)
    return filecount

  @classmethod
  def Append(cls, session, word, mail_id, compact=True):
    config = session.config
    sig = cls.WordSig(word)
    fd, fn = cls.GetFile(session, sig, mode='a')
    if (compact
    and (os.path.getsize(os.path.join(config.postinglist_dir(), fn)) >
             (1024*config.get('postinglist_kb', cls.MAX_SIZE))-(cls.HASH_LEN*6))
    and (random.randint(0, 50) == 1)):
      # This will compact the files and split out hot-spots, but we only bother
      # "once in a while" when the files are "big".
      fd.close()
      pls = cls(session, word)
      pls.append(mail_id)
      pls.save()
    else:
      # Quick and dirty append is the default.
      fd.write('%s\t%s\n' % (sig, mail_id))

  @classmethod
  def WordSig(cls, word):
    return strhash(word, cls.HASH_LEN)

  @classmethod
  def GetFile(cls, session, sig, mode='r'):
    sig = sig[:cls.HASH_LEN]
    while len(sig) > 0:
      fn = os.path.join(session.config.postinglist_dir(), sig)
      try:
        if os.path.exists(fn): return (cached_open(fn, mode), sig)
      except (IOError, OSError):
        pass

      if len(sig) > 1:
        sig = sig[:-1]
      else:
        if 'r' in mode:
          return (None, sig)
        else:
          return (cached_open(fn, mode), sig)
    # Not reached
    return (None, None)

  def __init__(self, session, word, sig=None, config=None):
    self.config = config or session.config
    self.session = session
    self.sig = sig or PostingList.WordSig(word)
    self.word = word
    self.WORDS = {self.sig: set()}
    self.load()

  def parse_line(self, line):
    words = line.strip().split('\t')
    if len(words) > 1:
      if words[0] not in self.WORDS: self.WORDS[words[0]] = set()
      self.WORDS[words[0]] |= set(words[1:])

  def load(self):
    self.size = 0
    fd, sig = PostingList.GetFile(self.session, self.sig)
    self.filename = sig
    if fd:
      try:
        for line in fd:
          self.size += len(line)
          if line.startswith(GPG_BEGIN_MESSAGE):
            for line in decrypt_gpg([line], fd):
              self.parse_line(line)
          else:
            self.parse_line(line)
      except ValueError:
        pass
      finally:
        fd.close()

  def fmt_file(self, prefix):
    output = []
    self.session.ui.mark('Formatting prefix %s' % unicode(prefix))
    for word in self.WORDS:
      if word.startswith(prefix) and len(self.WORDS[word]) > 0:
        output.append('%s\t%s\n' % (word,
                               '\t'.join(['%s' % x for x in self.WORDS[word]])))
    return ''.join(output)

  def save(self, prefix=None, compact=True, mode='w'):
    prefix = prefix or self.filename
    output = self.fmt_file(prefix)
    while (compact
    and    len(output) > 1024*self.config.get('postinglist_kb', self.MAX_SIZE)
    and    len(prefix) < self.HASH_LEN):
      biggest = self.sig
      for word in self.WORDS:
        if len(self.WORDS[word]) > len(self.WORDS[biggest]):
          biggest = word
      if len(biggest) > len(prefix):
        biggest = biggest[:len(prefix)+1]
        self.save(prefix=biggest, mode='a')

        for key in [k for k in self.WORDS if k.startswith(biggest)]:
          del self.WORDS[key]
        output = self.fmt_file(prefix)

    try:
      outfile = os.path.join(self.config.postinglist_dir(), prefix)
      if output:
        try:
          fd = cached_open(outfile, mode)
          fd.write(output)
          return len(output)
        finally:
          if mode != 'a': fd.close()
      elif os.path.exists(outfile):
        os.remove(outfile)
    except:
      self.session.ui.warning('%s' % (sys.exc_info(), ))
    return 0

  def hits(self):
    return self.WORDS[self.sig]

  def append(self, eid):
    self.WORDS[self.sig].add(eid)
    return self

  def remove(self, eid):
    try:
      self.WORDS[self.sig].remove(eid)
    except KeyError:
      pass
    return self


class MailIndex(object):
  """This is a lazily parsing object representing a mailpile index."""

  MSG_IDX     = 0
  MSG_PTRS    = 1
  MSG_UNUSED  = 2  # Was size, now reserved for other fun things
  MSG_ID      = 3
  MSG_DATE    = 4
  MSG_FROM    = 5
  MSG_SUBJECT = 6
  MSG_TAGS    = 7
  MSG_REPLIES = 8
  MSG_CONV_ID = 9

  def __init__(self, config):
    self.config = config
    self.STATS = {}
    self.INDEX = []
    self.PTRS = {}
    self.MSGIDS = {}
    self.CACHE = {}

  def l2m(self, line):
    return line.decode('utf-8').split(u'\t')

  def m2l(self, message):
    return (u'\t'.join([unicode(p) for p in message])).encode('utf-8')

  def load(self, session=None):
    self.INDEX = []
    self.PTRS = {}
    self.MSGIDS = {}
    if session: session.ui.mark('Loading metadata index...')
    try:
      fd = open(self.config.mailindex_file(), 'r')
      try:
        for line in fd:
          if line.startswith(GPG_BEGIN_MESSAGE):
            for line in decrypt_gpg([line], fd):
              line = line.strip()
              if line and not line.startswith('#'):
                self.INDEX.append(line)
          else:
            line = line.strip()
            if line and not line.startswith('#'):
              self.INDEX.append(line)
      except ValueError:
        pass
      fd.close()
    except IOError:
      if session: session.ui.warning(('Metadata index not found: %s'
                                      ) % self.config.mailindex_file())
    if session:
      session.ui.mark('Loaded metadata for %d messages' % len(self.INDEX))

  def save(self, session=None):
    if session: session.ui.mark("Saving metadata index...")
    fd = gpg_open(self.config.mailindex_file(),
                  self.config.get('gpg_recipient'), 'w')
    fd.write('# This is the mailpile.py index file.\n')
    fd.write('# We have %d messages!\n' % len(self.INDEX))
    for item in self.INDEX:
      fd.write(item + '\n')
    fd.close()
    flush_append_cache()
    if session: session.ui.mark("Saved metadata index")

  def update_ptrs_and_msgids(self, session):
    session.ui.mark('Updating high level indexes')
    for offset in range(0, len(self.INDEX)):
      message = self.l2m(self.INDEX[offset])
      if len(message) > self.MSG_CONV_ID:
        self.MSGIDS[message[self.MSG_ID]] = offset
        for msg_ptr in message[self.MSG_PTRS].split(','):
          self.PTRS[msg_ptr] = offset
      else:
        session.ui.warning('Bogus line: %s' % line)

  def try_decode(self, text, charset):
    for cs in (charset, 'iso-8859-1', 'utf-8'):
      if cs:
        try:
          return text.decode(cs)
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
          pass
    return "".join(i for i in text if ord(i)<128)

  def hdr(self, msg, name, value=None):
    try:
      decoded = email.header.decode_header(value or msg[name] or '')
      return (' '.join([self.try_decode(t[0], t[1]) for t in decoded])
              ).replace('\r', ' ').replace('\t', ' ').replace('\n', ' ')
    except email.errors.HeaderParseError:
      return ''

  def update_location(self, session, msg_idx, msg_ptr):
    msg_info = self.get_msg_by_idx(msg_idx)
    msg_ptrs = msg_info[self.MSG_PTRS].split(',')
    self.PTRS[msg_ptr] = msg_idx

    # If message was seen in this mailbox before, update the location
    for i in range(0, len(msg_ptrs)):
      if (msg_ptrs[i][:3] == msg_ptr[:3]):
        msg_ptrs[i] = msg_ptr
        msg_ptr = None
        break

    # Otherwise, this is a new mailbox, record this sighting as well!
    if msg_ptr: msg_ptrs.append(msg_ptr)
    msg_info[self.MSG_PTRS] = ','.join(msg_ptrs)
    self.set_msg_by_idx(msg_idx, msg_info)

  def scan_mailbox(self, session, idx, mailbox_fn, mailbox_opener):
    mbox = mailbox_opener(session, idx)
    session.ui.mark('%s: Checking: %s' % (idx, mailbox_fn))

    if mbox.last_parsed+1 == len(mbox): return 0

    if len(self.PTRS.keys()) == 0:
      self.update_ptrs_and_msgids(session)

    added = 0
    msg_date = int(time.time())
    for i in range(mbox.last_parsed+1, len(mbox)):
      if QUITTING: break
      parse_status = ('%s: Reading your mail: %d%% (%d/%d messages)'
                      ) % (idx, 100 * i/len(mbox), i, len(mbox))

      msg_ptr = mbox.get_msg_ptr(idx, i)
      if msg_ptr in self.PTRS:
        if (i % 317) == 0: session.ui.mark(parse_status)
        continue
      else:
        session.ui.mark(parse_status)

      # Message new or modified, let's parse it.
      p = email.parser.Parser()
      msg = p.parse(mbox.get_file(i))
      msg_id = b64c(sha1b64((self.hdr(msg, 'message-id') or msg_ptr).strip()))
      if msg_id in self.MSGIDS:
        self.update_location(session, self.MSGIDS[msg_id], msg_ptr)
        added += 1
      else:
        # Add new message!
        msg_mid = b36(len(self.INDEX))

        try:
          last_date = msg_date
          msg_date = int(rfc822.mktime_tz(
                                   rfc822.parsedate_tz(self.hdr(msg, 'date'))))
          if msg_date > (time.time() + 24*3600):
            session.ui.warning('=%s/%s is from the FUTURE!' % (msg_mid, msg_id))
            # Messages from the future are treated as today's
            msg_date = last_date + 1
        except (ValueError, TypeError, OverflowError):
          session.ui.warning('=%s/%s has a bogus date.' % (msg_mid, msg_id))
          # This is a hack: We assume the messages in the mailbox are in
          # chronological order and just add 1 second to the date of the last
          # message.  This should be a better-than-nothing guess.
          msg_date += 1

        msg_conv = None
        refs = set((self.hdr(msg, 'references')+' '+self.hdr(msg, 'in-reply-to')
                    ).replace(',', ' ').strip().split())
        for ref_id in [b64c(sha1b64(r)) for r in refs]:
          try:
            # Get conversation ID ...
            ref_mid = self.MSGIDS[ref_id]
            msg_conv = self.get_msg_by_idx(ref_mid)[self.MSG_CONV_ID]
            # Update root of conversation thread
            parent = self.get_msg_by_idx(int(msg_conv, 36))
            parent[self.MSG_REPLIES] += '%s,' % msg_mid
            self.set_msg_by_idx(int(msg_conv, 36), parent)
            break
          except (KeyError, ValueError, IndexError):
            pass
        if not msg_conv:
          # FIXME: If subject implies this is a reply, scan back a couple
          #        hundred messages for similar subjects - but not infinitely,
          #        conversations don't last forever.
          msg_conv = msg_mid

        keywords = self.index_message(session, msg_mid, msg_id, msg, msg_date,
                                      mailbox=idx, compact=False,
                                      filter_hooks=[self.filter_keywords])
        tags = [k.split(':')[0] for k in keywords if k.endswith(':tag')]

        self.set_msg_by_idx(len(self.INDEX),
                            [msg_mid,                   # Our index ID
                             msg_ptr,                   # Location on disk
                             '',                        # UNUSED
                             msg_id,                    # Message-ID
                             b36(msg_date),             # Date as a UTC timestamp
                             self.hdr(msg, 'from'),     # From:
                             self.hdr(msg, 'subject'),  # Subject
                             ','.join(tags),            # Initial tags
                             '',                        # No replies for now
                             msg_conv])                 # Conversation ID
        added += 1

    if added:
      mbox.last_parsed = i
      mbox.save(session)
    session.ui.mark('%s: Indexed mailbox: %s' % (idx, mailbox_fn))
    return added

  def filter_keywords(self, session, msg_mid, msg, keywords):
    keywordmap = {}
    msg_idx_list = [msg_mid]
    for kw in keywords:
      keywordmap[kw] = msg_idx_list

    for fid, terms, tags, comment in session.config.get_filters():
      if (terms == '*'
      or  len(self.search(None, terms.split(), keywords=keywordmap)) > 0):
        for t in tags.split():
          kw = '%s:tag' % t[1:]
          if t[0] == '-':
            if kw in keywordmap: del keywordmap[kw]
          else:
            keywordmap[kw] = msg_idx_list

    return set(keywordmap.keys())

  def index_message(self, session, msg_mid, msg_id, msg, msg_date,
                    mailbox=None, compact=True, filter_hooks=[]):
    keywords = []
    for part in msg.walk():
      charset = part.get_charset() or 'iso-8859-1'
      if part.get_content_type() == 'text/plain':
        textpart = self.try_decode(part.get_payload(None, True), charset)
      elif part.get_content_type() == 'text/html':
        payload = self.try_decode(part.get_payload(None, True), charset)
        if len(payload) > 3:
          try:
            textpart = lxml.html.fromstring(payload).text_content()
          except:
            session.ui.warning('=%s/%s has bogus HTML.' % (msg_mid, msg_id))
            textpart = payload
        else:
          textpart = payload
      else:
        textpart = None

      att = part.get_filename()
      if att:
        att = self.try_decode(att, charset)
        keywords.append('attachment:has')
        keywords.extend([t+':att' for t in re.findall(WORD_REGEXP, att.lower())])
        textpart = (textpart or '') + ' ' + att

      if textpart:
        # FIXME: Does this lowercase non-ASCII characters correctly?
        keywords.extend(re.findall(WORD_REGEXP, textpart.lower()))

    mdate = datetime.date.fromtimestamp(msg_date)
    keywords.append('%s:year' % mdate.year)
    keywords.append('%s:month' % mdate.month)
    keywords.append('%s:day' % mdate.day)
    keywords.append('%s-%s-%s:date' % (mdate.year, mdate.month, mdate.day))
    keywords.append('%s:id' % msg_id)
    keywords.extend(re.findall(WORD_REGEXP, self.hdr(msg, 'subject').lower()))
    keywords.extend(re.findall(WORD_REGEXP, self.hdr(msg, 'from').lower()))
    if mailbox: keywords.append('%s:mailbox' % mailbox.lower())

    for key in msg.keys():
      key_lower = key.lower()
      if key_lower not in BORING_HEADERS:
        words = set(re.findall(WORD_REGEXP, self.hdr(msg, key).lower()))
        words -= STOPLIST
        keywords.extend(['%s:%s' % (t, key_lower) for t in words])
        if 'list' in key_lower:
          keywords.extend(['%s:list' % t for t in words])

    keywords = set(keywords)
    keywords -= STOPLIST

    for hook in filter_hooks:
      keywords = hook(session, msg_mid, msg, keywords)

    for word in keywords:
      try:
        PostingList.Append(session, word, msg_mid, compact=compact)
      except UnicodeDecodeError:
        # FIXME: we just ignore garbage
        pass

    return keywords

  def get_msg_by_idx(self, msg_idx):
    try:
      if msg_idx not in self.CACHE:
        self.CACHE[msg_idx] = self.l2m(self.INDEX[msg_idx])
      return self.CACHE[msg_idx]
    except IndexError:
      return (None, None, None, None, b36(0),
              '(not in index)', '(not in index)', None, None)

  def set_msg_by_idx(self, msg_idx, msg_info):
    if msg_idx < len(self.INDEX):
      self.INDEX[msg_idx] = self.m2l(msg_info)
    elif msg_idx == len(self.INDEX):
      self.INDEX.append(self.m2l(msg_info))
    else:
      raise IndexError('%s is outside the index' % msg_idx)

    if msg_idx in self.CACHE:
      del(self.CACHE[msg_idx])

    self.MSGIDS[msg_info[self.MSG_ID]] = msg_idx
    for msg_ptr in msg_info[self.MSG_PTRS]:
      self.PTRS[msg_ptr] = msg_idx

  def get_conversation(self, msg_idx):
    return self.get_msg_by_idx(
             int(self.get_msg_by_idx(msg_idx)[self.MSG_CONV_ID], 36))

  def get_replies(self, msg_info=None, msg_idx=None):
    if not msg_info: msg_info = self.get_msg_by_idx(msg_idx)
    return [self.get_msg_by_idx(int(r, 36)) for r
            in msg_info[self.MSG_REPLIES].split(',') if r]

  def get_tags(self, msg_info=None, msg_idx=None):
    if not msg_info: msg_info = self.get_msg_by_idx(msg_idx)
    return [r for r in msg_info[self.MSG_TAGS].split(',') if r]

  def add_tag(self, session, tag_id, msg_info=None, msg_idxs=None):
    pls = PostingList(session, '%s:tag' % tag_id)
    if not msg_idxs:
      msg_idxs = [int(msg_info[self.MSG_IDX], 36)]
    session.ui.mark('Tagging %d messages (%s)' % (len(msg_idxs), tag_id))
    for msg_idx in list(msg_idxs):
      for reply in self.get_replies(msg_idx=msg_idx):
        if reply[self.MSG_IDX]:
          msg_idxs.add(int(reply[self.MSG_IDX], 36))
        if msg_idx % 1000 == 0: self.CACHE = {}
    for msg_idx in msg_idxs:
      msg_info = self.get_msg_by_idx(msg_idx)
      tags = set([r for r in msg_info[self.MSG_TAGS].split(',') if r])
      tags.add(tag_id)
      msg_info[self.MSG_TAGS] = ','.join(list(tags))
      self.INDEX[msg_idx] = self.m2l(msg_info)
      pls.append(msg_info[self.MSG_IDX])
      if msg_idx % 1000 == 0: self.CACHE = {}
    pls.save()
    self.CACHE = {}

  def remove_tag(self, session, tag_id, msg_info=None, msg_idxs=None):
    pls = PostingList(session, '%s:tag' % tag_id)
    if not msg_idxs:
      msg_idxs = [int(msg_info[self.MSG_IDX], 36)]
    session.ui.mark('Untagging conversations (%s)' % (tag_id, ))
    for msg_idx in list(msg_idxs):
      for reply in self.get_replies(msg_idx=msg_idx):
        if reply[self.MSG_IDX]:
          msg_idxs.add(int(reply[self.MSG_IDX], 36))
        if msg_idx % 1000 == 0: self.CACHE = {}
    session.ui.mark('Untagging %d messages (%s)' % (len(msg_idxs), tag_id))
    for msg_idx in msg_idxs:
      msg_info = self.get_msg_by_idx(msg_idx)
      tags = set([r for r in msg_info[self.MSG_TAGS].split(',') if r])
      if tag_id in tags:
        tags.remove(tag_id)
        msg_info[self.MSG_TAGS] = ','.join(list(tags))
        self.INDEX[msg_idx] = self.m2l(msg_info)
      pls.remove(msg_info[self.MSG_IDX])
      if msg_idx % 1000 == 0: self.CACHE = {}
    pls.save()
    self.CACHE = {}

  def search(self, session, searchterms, keywords=None):
    if keywords:
      def hits(term):
        return keywords.get(term, [])
    else:
      def hits(term):
        session.ui.mark('Searching for %s' % term)
        return PostingList(session, term).hits()

    if len(self.CACHE.keys()) > 5000: self.CACHE = {}
    r = []
    for term in searchterms:
      if term in STOPLIST:
        if session:
          session.ui.warning('Ignoring common word: %s' % term)
        continue

      if term[0] in ('-', '+'):
        op = term[0]
        term = term[1:]
      else:
        op = None

      r.append((op, []))
      rt = r[-1][1]
      term = term.lower()

      if term.startswith('body:'):
        rt.extend([int(h, 36) for h in hits(term[5:])])
      elif term == 'all:mail':
        rt.extend(range(0, len(self.INDEX)))
      elif ':' in term:
        t = term.split(':', 1)
        rt.extend([int(h, 36) for h in hits('%s:%s' % (t[1], t[0]))])
      else:
        rt.extend([int(h, 36) for h in hits(term)])

    if r:
      results = set(r[0][1])
      for (op, rt) in r[1:]:
        if op == '+':
          results |= set(rt)
        elif op == '-':
          results -= set(rt)
        else:
          results &= set(rt)
      # Sometimes the scan gets aborted...
      if not keywords:
        results -= set([len(self.INDEX)])
    else:
      results = set()

    if session:
      session.ui.mark('Found %d results' % len(results))
    return results

  def sort_results(self, session, results, how=None):
    force = how or False
    how = how or self.config.get('default_order', 'reverse_date')
    sign = how.startswith('rev') and -1 or 1
    sort_max = self.config.get('sort_max', 2500)
    if not results: return

    if len(results) > sort_max and not force:
      session.ui.warning(('Over sort_max (%s) results, sorting badly.'
                          ) % sort_max)
      results.sort()
      if sign < 0: results.reverse()
      leftovers = results[sort_max:]
      results[sort_max:] = []
    else:
      leftovers = []

    session.ui.mark('Sorting messages in %s order...' % how)
    try:
      if how == 'unsorted':
        pass
      elif how.endswith('index'):
        results.sort()
      elif how.endswith('random'):
        now = time.time()
        results.sort(key=lambda k: sha1b64('%s%s' % (now, k)))
      elif how.endswith('date'):
        results.sort(key=lambda k: long(self.get_msg_by_idx(k)[self.MSG_DATE], 36))
      elif how.endswith('from'):
        results.sort(key=lambda k: self.get_msg_by_idx(k)[self.MSG_FROM])
      elif how.endswith('subject'):
        results.sort(key=lambda k: self.get_msg_by_idx(k)[self.MSG_SUBJECT])
      else:
        session.ui.warning('Unknown sort order: %s' % how)
        results.extend(leftovers)
        return False
    except:
      session.ui.warning('Sort failed, sorting badly.  Partial index?')

    if sign < 0: results.reverse()

    if 'flat' not in how:
      conversations = [int(self.get_msg_by_idx(r)[self.MSG_CONV_ID], 36)
                       for r in results]
      results[:] = []
      chash = {}
      for c in conversations:
        if c not in chash:
          results.append(c)
          chash[c] = 1

    results.extend(leftovers)

    session.ui.mark('Sorted messages in %s order' % how)
    return True

  def update_tag_stats(self, session, config, update_tags=None):
    session = session or Session(config)
    new_tid = config.get_tag_id('new')
    new_msgs = (new_tid and PostingList(session, '%s:tag' % new_tid).hits()
                         or set([]))
    self.STATS.update({
      'ALL': [len(self.INDEX), len(new_msgs)]
    })
    for tid in (update_tags or config.get('tag', {}).keys()):
      if session: session.ui.mark('Counting messages in tag:%s' % tid)
      hits = PostingList(session, '%s:tag' % tid).hits()
      self.STATS[tid] = [len(hits), len(hits & new_msgs)]

    return self.STATS


##[ User Interface classes ]###################################################

class NullUI(object):

  WIDTH = 80
  interactive = False
  buffering = False

  def __init__(self):
    self.buffered = []

  def print_key(self, key, config): pass
  def reset_marks(self, quiet=False): pass
  def mark(self, progress): pass

  def flush(self):
    while len(self.buffered) > 0:
      self.buffered.pop(0)()

  def block(self):
    self.buffering = True

  def unblock(self):
    self.flush()
    self.buffering = False

  def say(self, text='', newline='\n', fd=sys.stdout):
    def sayit():
      fd.write(text.encode('utf-8')+newline)
      fd.flush()
    self.buffered.append(sayit)
    if not self.buffering: self.flush()

  def notify(self, message):
    self.say('%s%s' % (message, ' ' * (self.WIDTH-1-len(message))))
  def warning(self, message):
    self.say('Warning: %s%s' % (message, ' ' * (self.WIDTH-11-len(message))))
  def error(self, message):
    self.say('Error: %s%s' % (message, ' ' * (self.WIDTH-9-len(message))))

  def print_intro(self, help=False, http_worker=None):
    if http_worker:
      http_status = 'on: http://%s:%s/' % http_worker.httpd.sspec
    else:
      http_status = 'disabled.'
    self.say('\n'.join([ABOUT,
                        'The web interface is %s' % http_status,
                        '',
                        'For instructions type `help`, press <CTRL-D> to quit.',
                        '']))

  def print_help(self, commands, tags=None, index=None):
    self.say('Commands:')
    last_rank = None
    cmds = commands.keys()
    cmds.sort(key=lambda k: commands[k][3])
    for c in cmds:
      cmd, args, explanation, rank = commands[c]
      if not rank: continue

      if last_rank and int(rank/10) != last_rank: self.say()
      last_rank = int(rank/10)

      self.say('    %s|%-8.8s %-15.15s %s' % (c[0], cmd.replace('=', ''),
                                              args and ('<%s>' % args) or '',
                                              explanation))
    if tags and index:
      self.say('\nTags:  (use a tag as a command to display tagged messages)',
               '\n  ')
      tkeys = tags.keys()
      tkeys.sort(key=lambda k: tags[k])
      wrap = int(self.WIDTH / 23)
      for i in range(0, len(tkeys)):
        tid = tkeys[i]
        self.say(('%5.5s %-18.18s'
                  ) % ('%s' % (int(index.STATS.get(tid, [0, 0])[1]) or ''),
                       tags[tid]),
                 newline=(i%wrap)==(wrap-1) and '\n  ' or '')
    self.say('\n')

  def print_filters(self, config):
    w = int(self.WIDTH * 23/80)
    ffmt = ' %%3.3s %%-%d.%ds %%-%d.%ds %%s' % (w, w, w-2, w-2)
    self.say(ffmt % ('ID', ' Tags', 'Terms', ''))
    for fid, terms, tags, comment in config.get_filters():
      self.say(ffmt % (fid,
        ' '.join(['%s%s' % (t[0], config['tag'][t[1:]]) for t in tags.split()]),
                       (terms == '*') and '(all new mail)' or terms or '(none)',
                       comment or '(none)'))

  def display_messages(self, emails, raw=False, sep='', fd=sys.stdout):
    for email in emails:
      self.say(sep, fd=fd)
      if raw:
        for line in email.get_file().readlines():
          try:
            line = line.decode('utf-8')
          except UnicodeDecodeError:
            try:
              line = line.decode('iso-8859-1')
            except:
              line = '(MAILPILE DECODING FAILED)\n'
          self.say(line, newline='', fd=fd)
      else:
        for hdr in ('Date', 'To', 'From', 'Subject'):
          self.say('%s: %s' % (hdr, email.get(hdr, '(unknown)')), fd=fd)
        self.say('\n%s' % email.get_body_text(), fd=fd)


class TextUI(NullUI):
  def __init__(self):
    NullUI.__init__(self)
    self.times = []

  def print_key(self, key, config):
    if ':' in key:
      key, subkey = key.split(':', 1)
    else:
      subkey = None

    if key in config:
      if key in config.INTS:
        self.say('%s = %s (int)' % (key, config.get(key)))
      else:
        val = config.get(key)
        if subkey:
          if subkey in val:
            self.say('%s:%s = %s' % (key, subkey, val[subkey]))
          else:
            self.say('%s:%s is unset' % (key, subkey))
        else:
          self.say('%s = %s' % (key, config.get(key)))
    else:
      self.say('%s is unset' % key)

  def reset_marks(self, quiet=False):
    t = self.times
    self.times = []
    if t:
      if not quiet:
        result = 'Elapsed: %.3fs (%s)' % (t[-1][0] - t[0][0], t[-1][1])
        self.say('%s%s' % (result, ' ' * (self.WIDTH-1-len(result))))
      return t[-1][0] - t[0][0]
    else:
      return 0

  def mark(self, progress):
    self.say('  %s%s\r' % (progress, ' ' * (self.WIDTH-3-len(progress))),
             newline='', fd=sys.stderr)
    self.times.append((time.time(), progress))

  def name(self, sender):
    words = re.sub('["<>]', '', sender).split()
    nomail = [w for w in words if not '@' in w]
    if nomail: return ' '.join(nomail)
    return ' '.join(words)

  def names(self, senders):
    if len(senders) > 3:
      return re.sub('["<>]', '', ','.join([x.split()[0] for x in senders]))
    return ','.join([self.name(s) for s in senders])

  def compact(self, namelist, maxlen):
    l = len(namelist)
    while l > maxlen:
      namelist = re.sub(',[^, \.]+,', ',,', namelist, 1)
      if l == len(namelist): break
      l = len(namelist)
    namelist = re.sub(',,,+,', ' .. ', namelist, 1)
    return namelist

  def display_results(self, idx, results, terms,
                            start=0, end=None, num=None):
    if not results: return (0, 0)

    num = num or 20
    if end: start = end - num
    if start > len(results): start = len(results)
    if start < 0: start = 0

    clen = max(3, len('%d' % len(results)))
    cfmt = '%%%d.%ds' % (clen, clen)

    count = 0
    for mid in results[start:start+num]:
      count += 1
      try:
        msg_info = idx.get_msg_by_idx(mid)
        msg_subj = msg_info[idx.MSG_SUBJECT]

        msg_from = [msg_info[idx.MSG_FROM]]
        msg_from.extend([r[idx.MSG_FROM] for r in idx.get_replies(msg_info)])

        msg_date = [msg_info[idx.MSG_DATE]]
        msg_date.extend([r[idx.MSG_DATE] for r in idx.get_replies(msg_info)])
        msg_date = datetime.date.fromtimestamp(max([
                                                int(d, 36) for d in msg_date]))

        msg_tags = '<'.join(sorted([re.sub("^.*/", "", idx.config['tag'].get(t, t))
                                    for t in idx.get_tags(msg_info=msg_info)]))
        msg_tags = msg_tags and (' <%s' % msg_tags) or '  '

        sfmt = '%%-%d.%ds%%s' % (41-(clen+len(msg_tags)),41-(clen+len(msg_tags)))
        self.say((cfmt+' %4.4d-%2.2d-%2.2d %-25.25s '+sfmt
                  ) % (start + count,
                       msg_date.year, msg_date.month, msg_date.day,
                       self.compact(self.names(msg_from), 25),
                       msg_subj, msg_tags))
      except (IndexError, ValueError):
        self.say('-- (not in index: %s)' % mid)
    session.ui.mark(('Listed %d-%d of %d results'
                     ) % (start+1, start+count, len(results)))
    return (start, count)

  def display_messages(self, emails, raw=False, sep='', fd=None):
    if not fd and self.interactive:
      viewer = subprocess.Popen(['less'], stdin=subprocess.PIPE)
      fd = viewer.stdin
    else:
      fd = sys.stdout
      viewer = None
    try:
      NullUI.display_messages(self, emails, raw=raw, sep=('_' * self.WIDTH), fd=fd)
    except IOError, e:
      pass
    if viewer:
      fd.close()
      viewer.wait()


class HtmlUI(TextUI):

  WIDTH = 110

  def __init__(self):
    TextUI.__init__(self)
    self.buffered_html = []

  def say(self, text='', newline='\n', fd=None):
    if text.startswith('\r') and self.buffered_html:
      self.buffered_html[-1] = ('text', (text+newline).replace('\r', ''))
    else:
      self.buffered_html.append(('text', text+newline))

  def fmt(self, l):
    return l[1].replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')

  def render_html(self):
    html = ''.join([l[1] for l in self.buffered_html if l[0] == 'html'])
    html += '<br /><pre>%s</pre>' % ''.join([self.fmt(l)
                                             for l in self.buffered_html
                                             if l[0] != 'html'])
    self.buffered_html = []
    return html

  def display_results(self, idx, results, terms,
                            start=0, end=None, num=None):
    if not results: return (0, 0)

    num = num or 50
    if end: start = end - num
    if start > len(results): start = len(results)
    if start < 0: start = 0

    count = 0
    nav = []
    if start > 0:
      bstart = max(1, start-num+1)
      nav.append(('<a href="/?q=/search%s %s">&lt;&lt; page back</a>'
                  ) % (bstart > 1 and (' @%d' % bstart) or '', ' '.join(terms)))
    else:
      nav.append('first page')
    nav.append('(about %d results)' % len(results))
    if start+num < len(results):
      nav.append(('<a href="/?q=/search @%d %s">next page &gt;&gt;</a>'
                  ) % (start+num+1, ' '.join(terms)))
    else:
      nav.append('last page')
    self.buffered_html.append(('html', ('<p id="rnavtop" class="rnav">%s &nbsp;'
                                        ' </p>\n') % ' '.join(nav)))

    self.buffered_html.append(('html', '<table class="results">\n'))
    for mid in results[start:start+num]:
      count += 1
      try:
        msg_info = idx.get_msg_by_idx(mid)
        msg_subj = msg_info[idx.MSG_SUBJECT] or '(no subject)'

        msg_from = [msg_info[idx.MSG_FROM]]
        msg_from.extend([r[idx.MSG_FROM] for r in idx.get_replies(msg_info)])
        msg_from = msg_from or ['(no sender)']

        msg_date = [msg_info[idx.MSG_DATE]]
        msg_date.extend([r[idx.MSG_DATE] for r in idx.get_replies(msg_info)])
        msg_date = datetime.date.fromtimestamp(max([
                                                int(d, 36) for d in msg_date]))

        msg_tags = sorted([idx.config['tag'].get(t,t)
                           for t in idx.get_tags(msg_info=msg_info)
                           if 'tag:%s' % t not in terms])
        tag_classes = ['t_%s' % t.replace('/', '_') for t in msg_tags]
        msg_tags = ['<a href="/%s/">%s</a>' % (t, re.sub("^.*/", "", t))
                    for t in msg_tags]

        self.buffered_html.append(('html', (' <tr class="result %s %s">'
          '<td class="checkbox"><input type="checkbox" name="msg_%s" /></td>'
          '<td class="from"><a href="/=%s/%s/">%s</a></td>'
          '<td class="subject"><a href="/=%s/%s/">%s</a></td>'
          '<td class="tags">%s</td>'
          '<td class="date"><a href="?q=date:%4.4d-%d-%d">%4.4d-%2.2d-%2.2d</a></td>'
        '</tr>\n') % (
          (count % 2) and 'odd' or 'even', ' '.join(tag_classes).lower(),
          msg_info[idx.MSG_IDX],
          msg_info[idx.MSG_IDX], msg_info[idx.MSG_ID],
          self.compact(self.names(msg_from), 25),
          msg_info[idx.MSG_IDX], msg_info[idx.MSG_ID],
          msg_subj,
          ', '.join(msg_tags),
          msg_date.year, msg_date.month, msg_date.day,
          msg_date.year, msg_date.month, msg_date.day,
        )))
      except (IndexError, ValueError):
        pass
    self.buffered_html.append(('html', '</table>\n'))
    self.buffered_html.append(('html', ('<p id="rnavbot" class="rnav">%s &nbsp;'
                                        ' </p>\n') % ' '.join(nav)))
    session.ui.mark(('Listed %d-%d of %d results'
                     ) % (start+1, start+count, len(results)))
    return (start, count)


##[ Specialized threads ]######################################################

class Cron(threading.Thread):

  def __init__(self, name, session):
    threading.Thread.__init__(self)
    self.ALIVE = False
    self.name = name
    self.session = session
    self.schedule = {}
    self.sleep = 10

  def add_task(self, name, interval, task):
    self.schedule[name] = [name, interval, task, time.time()]
    self.sleep = 60
    for i in range(1, 61):
      ok = True
      for tn in self.schedule:
        if (self.schedule[tn][1] % i) != 0: ok = False
      if ok: self.sleep = i

  def cancel_task(self, name):
    if name in self.schedule:
      del self.schedule[name]

  def run(self):
    self.ALIVE = True
    while self.ALIVE and not QUITTING:
      now = time.time()
      for task_spec in self.schedule.values():
        name, interval, task, last = task_spec
        if task_spec[3] + task_spec[1] <= now:
          task_spec[3] = now
          task()

      # Some tasks take longer than others...
      delay = time.time() - now + self.sleep
      while delay > 0 and self.ALIVE:
        time.sleep(min(1, delay))
        delay -= 1

  def quit(self, session=None, join=True):
    self.ALIVE = False
    if join: self.join()


class Worker(threading.Thread):

  def __init__(self, name, session):
    threading.Thread.__init__(self)
    self.NAME = name or 'Worker'
    self.ALIVE = False
    self.JOBS = []
    self.LOCK = threading.Condition()
    self.pauses = 0
    self.session = session

  def add_task(self, session, name, task):
    self.LOCK.acquire()
    self.JOBS.append((session, name, task))
    self.LOCK.notify()
    self.LOCK.release()

  def do(self, session, name, task):
    if session and session.main:
      # We run this in the foreground on the main interactive session,
      # so CTRL-C has a chance to work.
      try:
        self.pause(session)
        rv = task()
        self.unpause(session)
      except:
        self.unpause(session)
        raise
    else:
      self.add_task(session, name, task)
      if session:
        rv = session.wait_for_task(name)
        if not rv:
          raise WorkerError()
      else:
        rv = True
    return rv

  def run(self):
    self.ALIVE = True
    while self.ALIVE and not QUITTING:
      self.LOCK.acquire()
      while len(self.JOBS) < 1:
        self.LOCK.wait()
      session, name, task = self.JOBS.pop(0)
      self.LOCK.release()

      try:
        if session:
          session.ui.mark('Starting: %s' % name)
          session.report_task_completed(name, task())
        else:
          task()
      except Exception, e:
        self.session.ui.error('%s failed in %s: %s' % (name, self.NAME, e))
        if session: session.report_task_failed(name)

  def pause(self, session):
    self.LOCK.acquire()
    self.pauses += 1
    if self.pauses == 1:
      self.LOCK.release()
      def pause_task():
        session.report_task_completed('Pause', True)
        session.wait_for_task('Unpause', quiet=True)
      self.add_task(None, 'Pause', pause_task)
      session.wait_for_task('Pause', quiet=True)
    else:
      self.LOCK.release()

  def unpause(self, session):
    self.LOCK.acquire()
    self.pauses -= 1
    if self.pauses == 0:
      session.report_task_completed('Unpause', True)
    self.LOCK.release()

  def die_soon(self, session=None):
    def die():
      self.ALIVE = False
    self.add_task(session, '%s shutdown' % self.NAME, die)

  def quit(self, session=None, join=True):
    self.die_soon(session=session)
    if join: self.join()


##[ Web and XML-RPC Interface ]###############################################

class HttpRequestHandler(SimpleXMLRPCRequestHandler):

  PAGE_HEAD = """\
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head>
 <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
 <script type='text/javascript'>
  function focus(eid) {var e = document.getElementById(eid);e.focus();
   if (e.setSelectionRange) {var l = 2*e.value.length;e.setSelectionRange(l,l)}
   else {e.value = e.value;}}
 </script>"""
  PAGE_LANDING_CSS = """\
 body {text-align: center; background: #f0fff0; color: #000; font-size: 2em; font-family: monospace; padding-top: 50px;}
 #heading a {text-decoration: none; color: #000;}
 #footer {text-align: center; font-size: 0.5em; margin-top: 15px;}
 #sidebar {display: none;}
 #search input {width: 170px;}"""
  PAGE_CONTENT_CSS = """\
 body {background: #f0fff0; font-family: monospace; color: #000;}
 body, div, form, h1, #header {padding: 0; margin: 0;}
 pre {display: inline-block; margin: 0 5px; padding: 0 5px;}
 #heading, #pile {padding: 5px 10px;}
 #heading {font-size: 3.75em; padding-left: 15px; padding-top: 15px; display: inline-block;}
 #heading a {text-decoration: none; color: #000;}
 #pile {z-index: -3; color: #666; font-size: 0.6em; position: absolute; top: 0; left: 0; text-align: center;}
 #search {display: inline-block;}
 #content {width: 80%; float: right;}
 #sidebar {width: 19%; float: left; overflow: hidden;}
 #sidebar .checked {font-weight: bold;}
 #sidebar ul.tag_list {list-style-type: none; white-space: nowrap; padding-left: 3em;}
 #sidebar .none {display: none;}
 #sidebar ul.tag_list input {position: absolute; margin: 0; margin-left: -1.5em;}
 #sidebar #sidebar_btns {display: inline-block; float: right;}
 #sidebar #sidebar_btns input {font-size: 0.8em; padding: 1px 2px; background: #d0dddd0; border: 1px solid #707770;}
 #sidebar #sidebar_btns input:hover {background: #e0eee0;}
 #footer {text-align: center; font-size: 0.8em; margin-top: 15px; clear: both;}
 p.rnav {margin: 4px 10px; text-align: center;}
 table.results {table-layout: fixed; border: 0; border-collapse: collapse; width: 100%; font-size: 13px; font-family: Helvetica,Arial;}
 tr.result td {overflow: hidden; white-space: nowrap; padding: 1px 3px; margin: 0;}
 tr.result td a {color: #000; text-decoration: none;}
 tr.result td a:hover {text-decoration: underline;}
 tr.result td.date a {color: #777;}
 tr.t_new {font-weight: bold;}
 #rnavtop {position: absolute; top: 0; right: 0;}
 td.date {width: 5em; font-size: 11px; text-align: center;}
 td.checkbox {width: 1.5em; text-align: center;}
 td.from {width: 25%; font-size: 12px;}
 td.tags {width: 12%; font-size: 11px; text-align: center;}
 tr.result td.tags a {color: #777;}
 tr.odd {background: #ffffff;}
 tr.even {background: #eeeeee;}
 #qbox {width: 400px;}"""
  PAGE_BODY = """
</head><body onload='focus("qbox");'><div id='header'>
 <h1 id='heading'>
  <a href='/'>M<span style='font-size: 0.8em;'>AILPILE</span>!</a></h1>
 <div id='search'><form action='/'>
  <input id='qbox' type='text' size='100' name='q' value='%(lastq)s ' />
  <input type='hidden' name='csrf' value='%(csrf)s' />
 </form></div>
 <p id='pile'>to: from:<br />subject: email<br />@ to: subject: list-id:<br />envelope
  from: to sender: spam to:<br />from: search GMail @ in-reply-to: GPG bounce<br />
  subscribe 419 v1agra from: envelope-to: @ SMTP hello!</p>
</div>
<form id='actions' action='' method='post'>
<input type='hidden' name='csrf' value='%(csrf)s' /><div id='content'>"""
  PAGE_SIDEBAR = """\
</div><div id='sidebar'>
 <div id='sidebar_btns'>
  <input id='rm_tag_btn' type='submit' name='rm_tag' value='un-' title='Untag messages' />
  <input id='add_tag_btn' type='submit' name='add_tag' value='tag' title='Tag messages' />
 </div>"""
  PAGE_TAIL = """\
</div><p id='footer'>&lt;
 <a href='https://github.com/pagekite/Mailpile'>free software</a>
 by <a title='Bjarni R. Einarsson' href='http://bre.klaki.net/'>bre</a>
&gt;</p>
</form></body></html>"""

  def send_standard_headers(self, header_list=[],
                            cachectrl='private', mimetype='text/html'):
    if mimetype.startswith('text/') and ';' not in mimetype:
      mimetype += ('; charset=utf-8')
    self.send_header('Cache-Control', cachectrl)
    self.send_header('Content-Type', mimetype)
    for header in header_list:
      self.send_header(header[0], header[1])
    self.end_headers()

  def send_full_response(self, message, code=200, msg='OK', mimetype='text/html',
                         header_list=[], suppress_body=False):
    message = unicode(message).encode('utf-8')
    self.log_request(code, message and len(message) or '-')
    self.wfile.write('HTTP/1.1 %s %s\r\n' % (code, msg))
    if code == 401:
      self.send_header('WWW-Authenticate',
                       'Basic realm=MP%d' % (time.time()/3600))
    self.send_header('Content-Length', len(message or ''))
    self.send_standard_headers(header_list=header_list, mimetype=mimetype)
    if not suppress_body:
      self.wfile.write(message or '')

  def csrf(self):
    ts = '%x' % int(time.time()/60)
    return '%s-%s' % (ts, b64w(sha1b64('-'.join([self.server.secret, ts]))))

  def render_page(self, body='', title=None, sidebar='', css=None,
                        variables=None):
    title = title or 'A huge pile of mail'
    variables = variables or {'lastq': '', 'path': '', 'csrf': self.csrf()}
    css = css or (body and self.PAGE_CONTENT_CSS or self.PAGE_LANDING_CSS)
    return '\n'.join([self.PAGE_HEAD % variables,
                      '<title>', title, '</title>',
                      '<style type="text/css">', css, '</style>',
                      self.PAGE_BODY % variables, body,
                      self.PAGE_SIDEBAR % variables, sidebar,
                      self.PAGE_TAIL % variables])

  def do_POST(self):
    (scheme, netloc, path, params, query, frag) = urlparse(self.path)
    if path.startswith('/::XMLRPC::/'):
      return SimpleXMLRPCRequestHandler.do_POST(self)

    post_data = { }
    try:
      clength = int(self.headers.get('content-length'))
      ctype, pdict = cgi.parse_header(self.headers.get('content-type'))
      if ctype == 'multipart/form-data':
        post_data = cgi.parse_multipart(self.rfile, pdict)
      elif ctype == 'application/x-www-form-urlencoded':
        if clength > 5*1024*1024:
          raise ValueError('OMG, input too big')
        post_data = cgi.parse_qs(self.rfile.read(clength), 1)
      else:
        raise ValueError('Unknown content-type')

    except (IOError, ValueError), e:
      body = 'POST geborked: %s' % e
      self.send_full_response(self.render_page(body=body,
                                               title='Internal Error'),
                              code=500)
      return None
    return self.do_GET(post_data=post_data)

  def do_HEAD(self):
    return self.do_GET(suppress_body=True)

  def parse_pqp(self, path, query_data, post_data, config):
    q = post_data.get('lq', query_data.get('q', ['']))[0].strip()

    cmd = ''
    if path.startswith('/_/'):
      cmd = ' '.join([path[3:], query_data.get('args', [''])[0]])
    elif path.startswith('/='):
      # FIXME: Should verify that the message ID matches!
      cmd = ' '.join(['view', path[1:].split('/')[0]])
    elif len(path) > 1:
      parts = path.split('/')[1:]
      if parts:
        fn = parts.pop()
        tid = self.server.session.config.get_tag_id('/'.join(parts))
        if tid:
          if q and q[0] != '/':
            q = 'tag:%s %s' % (tid, q)
          elif not q:
            q = 'tag:%s' % tid

    if q:
      if q[0] == '/':
        cmd = q[1:]
      else:
        tag = ''
        cmd = ''.join(['search ', tag, q])

    if 'add_tag' in post_data or 'rm_tag' in post_data:
      if 'add_tag' in post_data:
        fmt = 'tag +%s %s /%s'
      else:
        fmt = 'tag -%s %s /%s'
      msgs = ['='+k[4:] for k in post_data if k.startswith('msg_')]
      if msgs:
        for tid in [k[4:] for k in post_data if k.startswith('tag_')]:
          tname = config.get('tag', {}).get(tid)
          if tname:
            cmd = fmt % (tname, ' '.join(msgs), cmd)
    else:
      cmd = post_data.get('cmd', query_data.get('cmd', [cmd]))[0]

    return cmd.decode('utf-8')

  def do_GET(self, post_data={}, suppress_body=False):
    (scheme, netloc, path, params, query, frag) = urlparse(self.path)
    query_data = parse_qs(query)

    cmd = self.parse_pqp(path, query_data, post_data,
                         self.server.session.config)
    session = Session(self.server.session.config)
    session.ui = HtmlUI()
    index = session.config.get_index(session)

    if cmd:
      try:
        for arg in cmd.split(' /'):
          args = arg.strip().split()
          Action(session, args[0], ' '.join(args[1:]))
        body = session.ui.render_html()
        title = 'The biggest pile of mail EVAR!'
      except UsageError, e:
        body = 'Oops: %s' % e
        title = 'Ouch, too much mail, urgle, *choke*'
    else:
      body = ''
      title = None

    sidebar = ['<ul class="tag_list">']
    tids = index.config.get('tag', {}).keys()
    special = ['new', 'inbox', 'sent', 'drafts', 'spam', 'trash']
    def tord(k):
      tname = index.config['tag'][k]
      if tname.lower() in special:
        return '00000-%s-%s' % (special.index(tname.lower()), tname)
      return tname
    tids.sort(key=tord)
    for tid in tids:
      checked = ('tag:%s' % tid) in session.searched and ' checked' or ''
      checked1 = checked and ' checked="checked"' or ''
      tag_name = session.config.get('tag', {}).get(tid)
      tag_new = index.STATS.get(tid, [0,0])[1]
      sidebar.append((' <li id="tag_%s" class="%s">'
                      '<input type="checkbox" name="tag_%s"%s />'
                      ' <a href="/%s/">%s</a>'
                      ' <span class="tag_new %s">(<b>%s</b>)</span>'
                      '</li>') % (tid, checked, tid, checked1,
                                  tag_name, tag_name,
                                  tag_new and 'some' or 'none', tag_new))
    sidebar.append('</ul>')
    variables = {
      'lastq': post_data.get('lq', query_data.get('q',
                          [path != '/' and path[1] != '=' and path[:-1] or ''])
                             )[0].strip(),
      'csrf': self.csrf(),
      'path': path
    }
    self.send_full_response(self.render_page(body=body,
                                             title=title,
                                             sidebar='\n'.join(sidebar),
                                             variables=variables),
                            suppress_body=suppress_body)

  def log_message(self, fmt, *args):
    self.server.session.ui.notify(('HTTPD: '+fmt) % (args))


class HttpServer(SocketServer.ThreadingMixIn, SimpleXMLRPCServer):
  def __init__(self, session, sspec, handler):
    SimpleXMLRPCServer.__init__(self, sspec, handler)
    self.session = session
    self.sessions = {}
    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sspec = (sspec[0] or 'localhost', self.socket.getsockname()[1])
    # FIXME: This could be more securely random
    self.secret = '-'.join([str(x) for x in [self.socket, self.sspec,
                                             time.time(), self.session]])

  def finish_request(self, request, client_address):
    try:
      SimpleXMLRPCServer.finish_request(self, request, client_address)
    except socket.error:
      pass
    if QUITTING: self.shutdown()

class HttpWorker(threading.Thread):
  def __init__(self, session, sspec):
    threading.Thread.__init__(self)
    self.httpd = HttpServer(session, sspec, HttpRequestHandler)
    self.session = session

  def run(self):
    self.httpd.serve_forever()

  def quit(self):
    if self.httpd: self.httpd.shutdown()
    self.httpd = None


##[ The Configuration Manager ]###############################################

class ConfigManager(dict):

  background = None
  cron_worker = None
  http_worker = None
  slow_worker = None
  index = None

  MBOX_CACHE = {}
  RUNNING = {}

  INTS = ('postinglist_kb', 'sort_max', 'num_results', 'fd_cache_size',
          'http_port', 'rescan_interval')
  STRINGS = ('mailindex_file', 'postinglist_dir', 'default_order',
             'gpg_recipient', 'http_host', 'rescan_command')
  DICTS = ('mailbox', 'tag', 'filter', 'filter_terms', 'filter_tags')

  def workdir(self):
    return os.environ.get('MAILPILE_HOME', os.path.expanduser('~/.mailpile'))

  def conffile(self):
    return os.path.join(self.workdir(), 'config.rc')

  def parse_unset(self, session, arg):
    key = arg.strip().lower()
    if key in self:
      del self[key]
    elif ':' in key and key.split(':', 1)[0] in self.DICTS:
      key, subkey = key.split(':', 1)
      if key in self and subkey in self[key]:
        del self[key][subkey]
    session.ui.print_key(key, self)
    return True

  def parse_set(self, session, line):
    key, val = [k.strip() for k in line.split('=', 1)]
    key = key.lower()
    if key in self.INTS:
      try:
        self[key] = int(val)
      except ValueError:
        raise UsageError('%s is not an integer' % val)
    elif key in self.STRINGS:
      self[key] = val
    elif ':' in key and key.split(':', 1)[0] in self.DICTS:
      key, subkey = key.split(':', 1)
      if key not in self:
        self[key] = {}
      self[key][subkey] = val
    else:
      raise UsageError('Unknown key in config: %s' % key)
    session.ui.print_key(key, self)
    return True

  def parse_config(self, session, line):
    line = line.strip()
    if line.startswith('#') or not line:
      pass
    elif '=' in line:
      self.parse_set(session, line)
    else:
      raise UsageError('Bad line in config: %s' % line)

  def load(self, session):
    if not os.path.exists(self.workdir()):
      if session: session.ui.notify('Creating: %s' % self.workdir())
      os.mkdir(self.workdir())
    else:
      self.index = None
      for key in (self.INTS + self.STRINGS):
        if key in self: del self[key]
      try:
        fd = open(self.conffile(), 'r')
        try:
          for line in fd:
            if line.startswith(GPG_BEGIN_MESSAGE):
              for line in decrypt_gpg([line], fd):
                self.parse_config(session, line)
            else:
              self.parse_config(session, line)
        except ValueError:
          pass
        fd.close()
      except IOError:
        pass

  def save(self):
    if not os.path.exists(self.workdir()):
      session.ui.notify('Creating: %s' % self.workdir())
      os.mkdir(self.workdir())
    fd = gpg_open(self.conffile(), self.get('gpg_recipient'), 'w')
    fd.write('# Mailpile autogenerated configuration file\n')
    for key in sorted(self.keys()):
      if key in self.DICTS:
        for subkey in sorted(self[key].keys()):
          fd.write('%s:%s = %s\n' % (key, subkey, self[key][subkey]))
      else:
        fd.write('%s = %s\n' % (key, self[key]))
    fd.close()

  def nid(self, what):
    if what not in self or not self[what]:
      return '0'
    else:
      return b36(1+max([int(k, 36) for k in self[what]]))

  def clear_mbox_cache(self):
    self.MBOX_CACHE = {}

  def open_mailbox(self, session, mailbox_id):
    pfn = os.path.join(self.workdir(), 'pickled-mailbox.%s' % mailbox_id)
    for mid, mailbox_fn in self.get_mailboxes():
      if mid == mailbox_id:
        try:
          if mid in self.MBOX_CACHE:
            self.MBOX_CACHE[mid].update_toc()
          else:
            if session:
              session.ui.mark(('%s: Updating: %s'
                               ) % (mailbox_id, mailbox_fn))
            self.MBOX_CACHE[mid] = cPickle.load(open(pfn, 'r'))
        except (IOError, EOFError):
          if session:
            session.ui.mark(('%s: Opening: %s (may take a while)'
                             ) % (mailbox_id, mailbox_fn))
          mbox = IncrementalMbox(mailbox_fn)
          mbox.save(session, to=pfn)
          self.MBOX_CACHE[mid] = mbox
        return self.MBOX_CACHE[mid]
    raise IndexError('No such mailbox: %s' % mailbox_id)

  def get_filters(self):
    filters = self.get('filter', {}).keys()
    filters.sort(key=lambda k: int(k, 36))
    flist = []
    for fid in filters:
      comment = self.get('filter', {}).get(fid, '')
      terms = unicode(self.get('filter_terms', {}).get(fid, ''))
      tags = unicode(self.get('filter_tags', {}).get(fid, ''))
      flist.append((fid, terms, tags, comment))
    return flist

  def get_mailboxes(self):
    def fmt_mbxid(k):
      k = b36(int(k, 36))
      if len(k) > 3:
        raise ValueError('Mailbox ID too large: %s' % k)
      return ('000'+k)[-3:]
    mailboxes = self['mailbox'].keys()
    mailboxes.sort()
    return [(fmt_mbxid(k), self['mailbox'][k]) for k in mailboxes]

  def get_tag_id(self, tn):
    tn = tn.lower()
    tid = [t for t in self['tag'] if self['tag'][t].lower() == tn]
    return tid and tid[0] or None

  def history_file(self):
    return self.get('history_file',
                    os.path.join(self.workdir(), 'history'))

  def mailindex_file(self):
    return self.get('mailindex_file',
                    os.path.join(self.workdir(), 'mailpile.idx'))

  def postinglist_dir(self):
    d = self.get('postinglist_dir',
                 os.path.join(self.workdir(), 'search'))
    if not os.path.exists(d): os.mkdir(d)
    return d

  def get_index(self, session):
    if self.index: return self.index
    idx = MailIndex(self)
    idx.load(session)
    self.index = idx
    return idx

  def prepare_workers(config, session, daemons=False):
    # Set globals from config first...
    global APPEND_FD_CACHE_SIZE
    APPEND_FD_CACHE_SIZE = config.get('fd_cache_size',
                                      APPEND_FD_CACHE_SIZE)

    if not config.background:
      # Create a silent background session
      config.background = Session(config)
      config.background.ui = TextUI()
      config.background.ui.block()

    # Start the workers
    if not config.slow_worker:
      config.slow_worker = Worker('Slow worker', session)
      config.slow_worker.start()
    if daemons and not config.cron_worker:
      config.cron_worker = Cron('Cron worker', session)
      config.cron_worker.start()

      # Schedule periodic rescanning, if requested.
      rescan_interval = config.get('rescan_interval', None)
      if rescan_interval:
        def rescan():
          if 'rescan' not in config.RUNNING:
            config.slow_worker.add_task(None, 'Rescan',
                                        lambda: Action_Rescan(session, config))
        config.cron_worker.add_task('rescan', rescan_interval, rescan)

    if daemons and not config.http_worker:
      # Start the HTTP worker if requested
      sspec = (config.get('http_host', 'localhost'),
               config.get('http_port', DEFAULT_PORT))
      if sspec[0].lower() != 'disabled' and sspec[1] >= 0:
        config.http_worker = HttpWorker(session, sspec)
        config.http_worker.start()

  def stop_workers(config):
    for w in (config.http_worker, config.slow_worker, config.cron_worker):
      if w: w.quit()


##[ Sessions and User Commands ]###############################################

class Session(object):

  main = False
  interactive = False

  ui = NullUI()
  order = None

  def __init__(self, config):
    self.config = config
    self.wait_lock = threading.Condition()
    self.results = []
    self.searched = []
    self.displayed = (0, 0)
    self.task_results = []

  def report_task_completed(self, name, result):
    self.wait_lock.acquire()
    self.task_results.append((name, result))
    self.wait_lock.notify_all()
    self.wait_lock.release()

  def report_task_failed(self, name):
    self.report_task_completed(name, None)

  def wait_for_task(self, wait_for, quiet=False):
    while True:
      self.wait_lock.acquire()
      for i in range(0, len(self.task_results)):
        if self.task_results[i][0] == wait_for:
          tn, rv = self.task_results.pop(i)
          self.wait_lock.release()
          self.ui.reset_marks(quiet=quiet)
          return rv

      self.wait_lock.wait()
      self.wait_lock.release()

  def error(self, message):
    self.ui.error(message)
    if not self.interactive: sys.exit(1)


COMMANDS = {
  'A:': ('add=',     'path/to/mbox',  'Add a mailbox',                      60),
  'F:': ('filter=',  'options',       'Add/edit/delete auto-tagging rules', 56),
  'h':  ('help',     '',              'Print help on how to use mailpile',   0),
  'L':  ('load',     '',              'Load the metadata index',            61),
  'n':  ('next',     '',              'Display next page of results',       91),
  'o:': ('order=',   '[rev-]what',   ('Sort by: date, from, subject, '
                                      'random or index'),                   93),
  'O':  ('optimize', '',              'Optimize the keyword search index',  62),
  'p':  ('previous', '',              'Display previous page of results',   92),
  'P:': ('print=',   'var',           'Print a setting',                    52),
  'R':  ('rescan',   '',              'Scan all mailboxes for new messages',63),
  's:': ('search=',  'terms ...',     'Search!',                            90),
  'S:': ('set=',     'var=value',     'Change a setting',                   50),
  't:': ('tag=',     '[+|-]tag msg',  'Tag or untag search results',        94),
  'T:': ('addtag=',  'tag',           'Create a new tag',                   55),
  'U:': ('unset=',   'var',           'Reset a setting to the default',     51),
  'v:': ('view=',    '[raw] m1 ...',  'View one or more messages',          95),
  'W':  ('www',      '',              'Just run the web server',            56),
}
def Choose_Messages(session, words):
  msg_ids = set()
  for what in words:
    if what.lower() == 'these':
      b, c = session.displayed
      msg_ids |= set(session.results[b:b+c])
    elif what.lower() == 'all':
      msg_ids |= set(session.results)
    elif what.startswith('='):
      try:
        msg_ids.add(int(what[1:], 36))
      except ValueError:
        session.ui.warning('What message is %s?' % (what, ))
    elif '-' in what:
      try:
        b, e = what.split('-')
        msg_ids |= set(session.results[int(b)-1:int(e)])
      except:
        session.ui.warning('What message is %s?' % (what, ))
    else:
      try:
        msg_ids.add(session.results[int(what)-1])
      except:
        session.ui.warning('What message is %s?' % (what, ))
  return msg_ids

def Action_Load(session, config, reset=False, wait=True, quiet=False):
  if not reset and config.index:
    return config.index
  def do_load():
    if reset:
      config.index = None
      if session:
        session.results = []
        session.searched = []
        session.displayed = (0, 0)
    idx = config.get_index(session)
    idx.update_tag_stats(session, config)
    if session:
      session.ui.reset_marks(quiet=quiet)
    return idx
  if wait:
    return config.slow_worker.do(session, 'Load', do_load)
  else:
    config.slow_worker.add_task(session, 'Load', do_load)
    return None

def Action_Tag(session, opt, arg, save=True):
  idx = Action_Load(session, session.config)
  try:
    words = arg.split()
    op = words[0][0]
    tag = words[0][1:]
    tag_id = session.config.get_tag_id(tag)

    msg_ids = Choose_Messages(session, words[1:])
    if op == '-':
      idx.remove_tag(session, tag_id, msg_idxs=msg_ids)
    else:
      idx.add_tag(session, tag_id, msg_idxs=msg_ids)

    session.ui.reset_marks()

    if save:
      # Background save makes things feel fast!
      def background():
        idx.update_tag_stats(session, session.config)
        idx.save()
      session.config.slow_worker.add_task(None, 'Save index', background)
    else:
      idx.update_tag_stats(session, session.config)

    return True

  except (TypeError, ValueError, IndexError):
    session.ui.reset_marks()
    session.ui.error('That made no sense: %s %s' % (opt, arg))
    return False

def Action_Filter_Add(session, config, flags, args):
  terms = ('new' in flags) and ['*'] or session.searched
  if args and args[0][0] == '=':
    tag_id = args.pop(0)[1:]
  else:
    tag_id = config.nid('filter')

  if not terms or (len(args) < 1):
    raise UsageError('Need search term and flags')

  tags, tids = [], []
  while args and args[0][0] in ('-', '+'):
    tag = args.pop(0)
    tags.append(tag)
    tids.append(tag[0]+config.get_tag_id(tag[1:]))

  if not args:
    args = ['Filter for %s' % ' '.join(tags)]

  if 'notag' not in flags and 'new' not in flags:
    for tag in tags:
      if not Action_Tag(session, 'filter/tag', '%s all' % tag, save=False):
        raise UsageError()

  if (config.parse_set(session, ('filter:%s=%s'
                                 ) % (tag_id, ' '.join(args)))
  and config.parse_set(session, ('filter_tags:%s=%s'
                                 ) % (tag_id, ' '.join(tids)))
  and config.parse_set(session, ('filter_terms:%s=%s'
                                 ) % (tag_id, ' '.join(terms)))):
    session.ui.reset_marks()
    def save_filter():
      config.save()
      config.index.save(None)
    config.slow_worker.add_task(None, 'Save filter', save_filter)
  else:
    raise Exception('That failed, not sure why?!')

def Action_Filter_Delete(session, config, flags, args):
  if len(args) < 1 or args[0] not in config.get('filter', {}):
    raise UsageError('Delete what?')

  fid = args[0]
  if (config.parse_unset(session, 'filter:%s' % fid)
  and config.parse_unset(session, 'filter_tags:%s' % fid)
  and config.parse_unset(session, 'filter_terms:%s' % fid)):
    config.save()
  else:
    raise Exception('That failed, not sure why?!')

def Action_Filter_Move(session, config, flags, args):
  raise Exception('Unimplemented')

def Action_Filter(session, opt, arg):
  config = session.config
  args = arg.split()
  flags = []
  while args and args[0] in ('add', 'set', 'delete', 'move', 'list',
                             'new', 'notag'):
    flags.append(args.pop(0))
  try:
    if 'delete' in flags:
      return Action_Filter_Delete(session, config, flags, args)
    elif 'move' in flags:
      return Action_Filter_Move(session, config, flags, args)
    elif 'list' in flags:
      return session.ui.print_filters(config)
    else:
      return Action_Filter_Add(session, config, flags, args)
  except UsageError:
    pass
  except Exception, e:
    session.error(e)
    return
  session.ui.say(
    'Usage: filter [new] [notag] [=ID] <[+|-]tags ...> [description]\n'
    '       filter delete <id>\n'
    '       filter move <id> <pos>\n'
    '       filter list')

def Action_Rescan(session, config):
  if 'rescan' in config.RUNNING: return
  config.RUNNING['rescan'] = True
  idx = config.index
  count = 0
  try:
    pre_command = config.get('rescan_command', None)
    if pre_command:
      session.ui.mark('Running: %s' % pre_command)
      subprocess.check_call(pre_command, shell=True)
    count = 1
    for fid, fpath in config.get_mailboxes():
      if QUITTING: break
      count += idx.scan_mailbox(session, fid, fpath, config.open_mailbox)
      config.clear_mbox_cache()
      session.ui.mark('\n')
    count -= 1
    if not count: session.ui.mark('Nothing changed')
  except (KeyboardInterrupt, subprocess.CalledProcessError), e:
    session.ui.mark('Aborted: %s' % e)
  finally:
    if count:
      session.ui.mark('\n')
      idx.save(session)
  idx.update_tag_stats(session, config)
  session.ui.reset_marks()
  del config.RUNNING['rescan']
  return True

def Action_Optimize(session, config, arg):
  try:
    idx = config.index
    filecount = PostingList.Optimize(session, idx,
                                     force=(arg == 'harder'))
    session.ui.reset_marks()
  except KeyboardInterrupt:
    session.ui.mark('Aborted')
    session.ui.reset_marks()
  return True

def Action(session, opt, arg):
  config = session.config
  session.ui.reset_marks(quiet=True)
  num_results = config.get('num_results', None)

  if not opt or opt in ('h', 'help'):
    session.ui.print_help(COMMANDS, tags=session.config.get('tag', {}),
                                    index=config.get_index(session))

  elif opt in ('W', 'webserver'):
    config.prepare_workers(session, daemons=True)
    while not QUITTING: time.sleep(1)

  elif opt in ('A', 'add'):
    if os.path.exists(arg):
      arg = os.path.abspath(arg)
      if config.parse_set(session,
                          'mailbox:%s=%s' % (config.nid('mailbox'), arg)):
        config.slow_worker.add_task(None, 'Save config', lambda: config.save())
    else:
      session.error('No such file/directory: %s' % arg)

  elif opt in ('T', 'addtag'):
    if (arg
    and ' ' not in arg
    and arg.lower() not in [v.lower() for v in config['tag'].values()]):
      if config.parse_set(session,
                          'tag:%s=%s' % (config.nid('tag'), arg)):
        config.slow_worker.add_task(None, 'Save config', lambda: config.save())
    else:
      session.error('Invalid tag: %s' % arg)

  elif opt in ('F', 'filter'):
    Action_Filter(session, opt, arg)

  elif opt in ('O', 'optimize'):
    config.slow_worker.do(session, 'Optimize',
                          lambda: Action_Optimize(session, config, arg))

  elif opt in ('P', 'print'):
    session.ui.print_key(arg.strip().lower(), config)

  elif opt in ('U', 'unset'):
    if config.parse_unset(session, arg):
      config.slow_worker.add_task(None, 'Save config', lambda: config.save())

  elif opt in ('S', 'set'):
    if config.parse_set(session, arg):
      config.slow_worker.add_task(None, 'Save config', lambda: config.save())

  elif opt in ('R', 'rescan'):
    Action_Load(session, config)
    config.slow_worker.do(session, 'Rescan',
                          lambda: Action_Rescan(session, config))

  elif opt in ('L', 'load'):
    Action_Load(session, config, reset=True)

  elif opt in ('n', 'next'):
    idx = Action_Load(session, config)
    session.ui.reset_marks()
    pos, count = session.displayed
    session.displayed = session.ui.display_results(idx, session.results,
                                                   session.searched,
                                                   start=pos+count,
                                                   num=num_results)
    session.ui.reset_marks()

  elif opt in ('p', 'previous'):
    idx = Action_Load(session, config)
    pos, count = session.displayed
    session.displayed = session.ui.display_results(idx, session.results,
                                                   session.searched,
                                                   end=pos,
                                                   num=num_results)
    session.ui.reset_marks()

  elif opt in ('t', 'tag'):
    Action_Tag(session, opt, arg)

  elif opt in ('o', 'order'):
    idx = Action_Load(session, config)
    session.order = arg or None
    idx.sort_results(session, session.results,
                     how=session.order)
    session.displayed = session.ui.display_results(idx, session.results,
                                                   session.searched,
                                                   num=num_results)
    session.ui.reset_marks()

  elif (opt in ('s', 'search')
        or opt.lower() in [t.lower() for t in config['tag'].values()]):
    idx = Action_Load(session, config)

    # FIXME: This is all rather dumb.  Make it smarter!

    session.searched = []
    if opt not in ('s', 'search'):
      tid = config.get_tag_id(opt)
      session.searched = ['tag:%s' % tid[0]]

    if arg.startswith('@'):
      try:
        if ' ' in arg:
          args = arg[1:].split(' ')
          start = args.pop(0)
        else:
          start, args = arg[1:], []
        start = int(start)-1
        arg = ' '.join(args)
      except ValueError:
        raise UsageError('Weird starting point')
    else:
      start = 0

    if ':' in arg or '-' in arg or '+' in arg:
      session.searched.extend(arg.lower().split())
    else:
      session.searched.extend(re.findall(WORD_REGEXP, arg.lower()))

    session.results = list(idx.search(session, session.searched))
    idx.sort_results(session, session.results, how=session.order)
    session.displayed = session.ui.display_results(idx, session.results,
                                                   session.searched,
                                                   start=start,
                                                   num=num_results)
    session.ui.reset_marks()

  elif opt in ('v', 'view'):
    args = arg.split()
    if args and args[0].lower() == 'raw':
      raw = args.pop(0)
    else:
      raw = False
    idx = Action_Load(session, config)
    emails = [Email(idx, i) for i in Choose_Messages(session, args)]
    session.ui.display_messages(emails, raw=raw)
    session.ui.reset_marks()

  else:
    raise UsageError('Unknown command: %s' % opt)


def Interact(session):
  import readline
  try:
    readline.read_history_file(session.config.history_file())
  except IOError:
    pass
  readline.set_history_length(100)

  try:
    while True:
      session.ui.block()
      opt = raw_input('mailpile> ').decode('utf-8').strip()
      session.ui.unblock()
      if opt:
        if ' ' in opt:
          opt, arg = opt.split(' ', 1)
        else:
          arg = ''
        try:
          Action(session, opt, arg)
        except UsageError, e:
          session.error(str(e))
  except EOFError:
    print

  readline.write_history_file(session.config.history_file())


##[ Main ]####################################################################

def Main(args):
  re.UNICODE = 1
  re.LOCALE = 1

  try:
    # Create our global config manager and the default (CLI) session
    config = ConfigManager()
    session = Session(config)
    session.config.load(session)
    session.main = True
    session.ui = TextUI()
  except AccessError, e:
    sys.stderr.write('Access denied: %s\n' % e)
    sys.exit(1)

  try:
    # Create and start (most) worker threads
    config.prepare_workers(session)

    try:
      opts, args = getopt.getopt(args,
                                 ''.join(COMMANDS.keys()),
                                 [v[0] for v in COMMANDS.values()])
      for opt, arg in opts:
        Action(session, opt.replace('-', ''), arg)
      if args:
        Action(session, args[0], ' '.join(args[1:]))

    except (getopt.GetoptError, UsageError), e:
      session.error(e)


    if not opts and not args:
      # Create and start the rest of the threads, load the index.
      config.prepare_workers(session, daemons=True)
      Action_Load(session, config, quiet=True)
      session.interactive = session.ui.interactive = True
      session.ui.print_intro(help=True, http_worker=config.http_worker)
      Interact(session)

  except KeyboardInterrupt:
    pass

  finally:
    QUITTING = True
    config.stop_workers()

if __name__ == "__main__":
  Main(sys.argv[1:])
