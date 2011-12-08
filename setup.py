#!/usr/bin/python
from datetime import date
from setuptools import setup
from mailpile import APPVER
import os

try:
  # This borks sdist.
  os.remove('.SELF')
except:
  pass

setup(
    name="mailpile",
    version=APPVER.replace('github',
                           'dev'+date.today().isoformat().replace('-', '')),
    license="AGPLv3+",
    author="Bjarni R. Einarsson",
    author_email="bre@klaki.net",
    url="http://mailpile.pagekite.me/",
    description="""Mailpile is a personal tool for searching and indexing e-mail.""",
    long_description="""\
Mailpile is a tool for building and maintaining a tagging search
engine for a personal collection of e-mail.
""",
   packages=['mailpile'],
   scripts=['scripts/pagekite'],
)
