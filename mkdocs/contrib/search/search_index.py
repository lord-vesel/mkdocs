from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from mkdocs.structure.pages import Page
from mkdocs.structure.toc import AnchorLink, TableOfContents

try:
    from lunr import lunr

    haslunrpy = True
except ImportError:
    haslunrpy = False

log = logging.getLogger(__name__)


class SearchIndex:
    """
    Search index is a collection of pages and sections (heading
    tags and their following content are sections).
    """

    def __init__(self, **config) -> None:
        self._entries: List[dict] = []
        self.config = config

    def _find_toc_by_id(self, toc, id_: Optional[str]) -> Optional[AnchorLink]:
        """
        Given a table of contents and HTML ID, iterate through
        and return the matched item in the TOC.
        """
        for toc_item in toc:
            if toc_item.id == id_:
                return toc_item
            toc_item_r = self._find_toc_by_id(toc_item.children, id_)
            if toc_item_r is not None:
                return toc_item_r
        return None

    def _add_entry(self, title: Optional[str], text: str, loc: str) -> None:
        """
        A simple wrapper to add an entry, dropping bad characters.
        """
        text = text.replace('\u00a0', ' ')
        text = re.sub(r'[ \t\n\r\f\v]+', ' ', text.strip())

        self._entries.append({'title': title, 'text': text, 'location': loc})

    def add_entry_from_context(self, page: Page) -> None:
        """
        Create a set of entries in the index for a page. One for
        the page itself and then one for each of its' heading
        tags.
        """
        title_parts = [page.title] if page.title else []
        if self.config['full_path_in_title']:
            for ancestor in page.ancestors:
                title_parts.insert(0, ancestor.title)
        page_title = ' / '.join(title_parts)
        # Create the content parser and feed in the HTML for the
        # full page. This handles all the parsing and prepares
        # us to iterate through it.
        parser = ContentParser()
        assert page.content is not None
        parser.feed(page.content)
        parser.close()

        # Get the absolute URL for the page, this is then
        # prepended to the urls of the sections
        url = page.url

        # Create an entry for the full page.
        text = parser.stripped_html.rstrip('\n') if self.config['indexing'] == 'full' else ''

        self._add_entry(title=page_title, text=text, loc=url)

        if self.config['indexing'] in ['full', 'sections']:
            for i, section in enumerate(parser.data):
                if self.config['full_path_in_title'] and i == 0:
                    section_title = page_title
                else:
                    section_title = None
                self.create_entry_for_section(section, page.toc, url, section_title)

    def create_entry_for_section(
        self,
        section: ContentSection,
        toc: TableOfContents,
        abs_url: str,
        title: Optional[str] = None,
    ) -> None:
        """
        Given a section on the page, the table of contents and
        the absolute url for the page create an entry in the
        index
        """
        toc_item = self._find_toc_by_id(toc, section.id)

        text = ' '.join(section.text) if self.config['indexing'] == 'full' else ''
        if toc_item is not None:
            self._add_entry(title=title or toc_item.title, text=text, loc=abs_url + toc_item.url)

    def generate_search_index(self) -> str:
        """python to json conversion"""
        page_dicts = {'docs': self._entries, 'config': self.config}
        data = json.dumps(page_dicts, sort_keys=True, separators=(',', ':'), default=str)

        if self.config['prebuild_index'] in (True, 'node'):
            try:
                script_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), 'prebuild-index.js'
                )
                p = subprocess.Popen(
                    ['node', script_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding='utf-8',
                )
                idx, err = p.communicate(data)
                if not err:
                    page_dicts['index'] = json.loads(idx)
                    data = json.dumps(page_dicts, sort_keys=True, separators=(',', ':'))
                    log.debug('Pre-built search index created successfully.')
                else:
                    log.warning(f'Failed to pre-build search index. Error: {err}')
            except (OSError, ValueError) as e:
                log.warning(f'Failed to pre-build search index. Error: {e}')
        elif self.config['prebuild_index'] == 'python':
            if haslunrpy:
                lunr_idx = lunr(
                    ref='location',
                    fields=('title', 'text'),
                    documents=self._entries,
                    languages=self.config['lang'],
                )
                page_dicts['index'] = lunr_idx.serialize()
                data = json.dumps(page_dicts, sort_keys=True, separators=(',', ':'))
            else:
                log.warning(
                    "Failed to pre-build search index. The 'python' method was specified; "
                    "however, the 'lunr.py' library does not appear to be installed. Try "
                    "installing it with 'pip install lunr'. If you are using any language "
                    "other than English you will also need to install 'lunr[languages]'."
                )

        return data


class ContentSection:
    """
    Used by the ContentParser class to capture the information we
    need when it is parsing the HTML.
    """

    def __init__(
        self,
        text: Optional[List[str]] = None,
        id_: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        self.text = text or []
        self.id = id_
        self.title = title

    def __eq__(self, other):
        return self.text == other.text and self.id == other.id and self.title == other.title


_HEADER_TAGS = tuple(f"h{x}" for x in range(1, 7))


class ContentParser(HTMLParser):
    """
    Given a block of HTML, group the content under the preceding
    heading tags which can then be used for creating an index
    for that section.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.data: List[ContentSection] = []
        self.section: Optional[ContentSection] = None
        self.is_header_tag = False
        self._stripped_html: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Called at the start of every HTML tag."""

        # We only care about the opening tag for headings.
        if tag not in _HEADER_TAGS:
            return

        # We are dealing with a new header, create a new section
        # for it and assign the ID if it has one.
        self.is_header_tag = True
        self.section = ContentSection()
        self.data.append(self.section)

        for attr in attrs:
            if attr[0] == "id":
                self.section.id = attr[1]

    def handle_endtag(self, tag: str) -> None:
        """Called at the end of every HTML tag."""

        # We only care about the opening tag for headings.
        if tag not in _HEADER_TAGS:
            return

        self.is_header_tag = False

    def handle_data(self, data: str) -> None:
        """
        Called for the text contents of each tag.
        """
        self._stripped_html.append(data)

        if self.section is None:
            # This means we have some content at the start of the
            # HTML before we reach a heading tag. We don't actually
            # care about that content as it will be added to the
            # overall page entry in the search. So just skip it.
            return

        # If this is a header, then the data is the title.
        # Otherwise it is content of something under that header
        # section.
        if self.is_header_tag:
            self.section.title = data
        else:
            self.section.text.append(data.rstrip('\n'))

    @property
    def stripped_html(self) -> str:
        return '\n'.join(self._stripped_html)
