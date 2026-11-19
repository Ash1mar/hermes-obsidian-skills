"""Qualify incremental page support against the frozen Maps and registered units."""
from collections import defaultdict
import re
import unicodedata
from urllib.parse import unquote

from hermes_source_units.incremental_pages import require, unique_refs
from hermes_source_units.reading_dependencies import required_reading
from hermes_source_units.validation import canonical_json, fingerprint

QUALIFIERS = {'condition', 'exception', 'conflict', 'contrary', 'definition'}
CONTENTS = re.compile(r'^(?:目\s*[次录錄]|(?:table of )?contents?)$', re.I)
ENTRY = re.compile(r'^\s*(?:[#>*-]+\s*)?(\d+(?:[.．]\d+)*)\s+(.+?)\s*(?:[.．…·]{2,}\s*\(?\d+\)?\s*)$', re.M)
HEADING = re.compile(r'^(\d+(?:[.．]\d+)*)\s+(.+)$')
ATX_HEADING = re.compile(r'^#{1,6}\s+(.+?)(?:\s+#+)?$')
STATEMENT = re.compile(
    r'不[应得宜]|严禁|禁止|必须|须(?:按|设|为|符合)|宜(?:采用|设置|符合|为)|'
    r'应(?:当|为|按|设|采用|符合|满足|在|由|保持|设置|能|有|大于|小于)|'
    r'是指|系指|指由|定义[为：:]|[为是][ \t]*\d|'
    r'\b(?:shall|must|should|means|is defined as|is required to)\b', re.I)
CURRENT_WRITING_REVISION = 2


def normalized(text):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text).casefold())


class EvidenceCatalogue:
    def __init__(self, service, maps, actor):
        self.source = service.source
        self.access = {'actor': actor, 'purpose': 'construction',
                       'registry_revision': service._document_registry_revision()}
        self.units, self.aliases, self.sections, self.topics = {}, {}, {}, []
        self.asset_paths = {}
        self._live = {}
        for mapped in maps:
            aliases = mapped['alias_table']
            if not aliases:
                continue
            pin = next(iter(aliases.values()))['unit_ref']
            _, units, sections = self.source._load_set(pin['resource_id'], pin['unit_set_id'])
            key = mapped['snapshot_id']
            self.sections[key] = {row['section_id']: row for row in sections}
            self.asset_paths[key] = {unit['locator']['path'] for unit in units if unit['locator']['kind'] == 'asset'}
            registered = {canonical_json(row['ref']): row for row in units}
            coverage = {canonical_json(row['unit_ref']): row['disposition'] for row in mapped['coverage']}
            for alias, row in aliases.items():
                ref_key = canonical_json(row['unit_ref'])
                unit = registered.get(ref_key)
                require(alias not in self.aliases and ref_key not in self.units and unit
                        and key == row['snapshot_id'] and row['sha256'] == unit['content_sha256'],
                        'Map alias differs from its registered source snapshot')
                item = {'alias': alias, 'snapshot': key, 'section': unit['section_id'],
                        'locator': unit['locator'], 'sha256': row['sha256'],
                        'ref': {'unit_ref': unit['ref'], 'span': None}, 'coverage': coverage[ref_key]}
                self.aliases[alias] = self.units[ref_key] = item
            self.topics.extend((mapped['map_result_id'], key, tid, topic)
                               for tid, topic in sorted(mapped['mapped']['topics'].items()))

    def lineage(self, item):
        sections = self.sections[item['snapshot']]
        section_id = item['section']
        while section_id:
            section = sections[section_id]
            yield section
            section_id = section['parent_id']

    def label(self, alias):
        item = self.aliases[alias]
        headings = list(self.lineage(item))
        title = headings[0]['title']
        if item['locator']['kind'] == 'asset':
            kind = 'table' if 'tables/' in item['locator']['path'] else 'asset'
        elif any(CONTENTS.fullmatch(section['title'].strip()) or ENTRY.fullmatch(section['title'].strip())
                 for section in headings):
            kind = 'navigation'
        else:
            kind = 'source_text'
        return [kind, title[:100]]

    def read(self, item):
        alias = item['alias']
        if alias not in self._live:
            live = self.source.get({'source_ref': item['ref'], 'access': self.access})
            require(live['content_sha256'] == item['sha256'], 'live page support differs from the sealed Map')
            self._live[alias] = live['core_text'] or ''
        return self._live[alias]

    def body(self, item):
        if self.label(item['alias'])[0] != 'source_text':
            return False
        text = re.sub(r'<!--.*?-->', '', self.read(item), flags=re.S).strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or all(ENTRY.fullmatch(line) for line in lines):
            return False
        # A heading can itself be a complete numbered clause in the conversion.
        return any(not line.startswith('#') or re.match(r'^#+\s+\d+(?:[.．]\d+){2,}\s+.+', line)
                   for line in lines)

    def writing_body(self, item):
        """Distinguish a structural label from a clause rendered as a heading."""
        if self.label(item['alias'])[0] != 'source_text':
            return False
        section = self.sections[item['snapshot']][item['section']]
        text = re.sub(r'<!--.*?-->', '', self.read(item), flags=re.S).strip()
        for line in map(str.strip, text.splitlines()):
            if not line or ENTRY.fullmatch(line) or re.fullmatch(r'[-=]{2,}', line):
                continue
            heading = ATX_HEADING.fullmatch(line)
            if heading or normalized(line) == normalized(section['title']):
                title = heading[1] if heading else line
                if STATEMENT.search(title) or (HEADING.fullmatch(title) and title.endswith(('。', '！', '？'))):
                    return True
                continue
            return True
        return False

    def chapter_support(self, page_name, selected, has_body):
        """Resolve a uniquely named numbered entry through the same Map and chapter."""
        anchors = set()
        page = normalized(page_name)
        for item in selected:
            if self.label(item['alias'])[0] != 'navigation':
                continue
            for entry in ENTRY.finditer(self.read(item)):
                number, title = entry.groups()
                label = normalized(title)
                if label and label in page:
                    anchors.add((item['snapshot'], number.replace('．', '.'), label))
        if len(anchors) != 1:
            return None
        snapshot, number, label = next(iter(anchors))
        roots = []
        for section in self.sections[snapshot].values():
            heading = HEADING.fullmatch(section['title'].strip())
            if heading and heading[1].replace('．', '.') == number and normalized(heading[2]) == label:
                roots.append(section['section_id'])
        # Prefer the exact registered clause/chapter identifier when the outline
        # also contains a separately registered commentary with the same title.
        if number in roots:
            roots = [number]
        if len(roots) != 1:
            return None
        root, = roots
        candidates = []
        for map_id, source, tid, topic in self.topics:
            if source != snapshot or normalized(topic['name']) != label:
                continue
            relations = [r for r in topic['relations'] if r['role'] != 'context']
            aliases = list(dict.fromkeys(r['item'] for r in relations
                           if self.aliases[r['item']]['coverage'] == 'mapped'
                           and root in {s['section_id'] for s in self.lineage(self.aliases[r['item']])}
                           and has_body(self.aliases[r['item']])))
            if aliases:
                candidates.append((map_id, tid, aliases))
        if len(candidates) != 1:
            return None
        map_id, tid, aliases = candidates[0]
        return {'map_result_id': map_id, 'topic_id': tid, 'snapshot_id': snapshot,
                'section_id': root, 'aliases': aliases}

    def section_support(self, selected):
        """Locate mapped body only under an explicitly selected registered section."""
        anchors = {}
        for item in selected:
            if self.label(item['alias'])[0] != 'source_text' or self.writing_body(item):
                continue
            section = self.sections[item['snapshot']][item['section']]
            title = section['title']
            if not HEADING.fullmatch(title):
                continue
            raw = re.sub(r'<!--.*?-->', '', self.read(item), flags=re.S)
            labels = [heading[1] if (heading := ATX_HEADING.fullmatch(line.strip())) else line.strip()
                      for line in raw.splitlines() if line.strip()]
            if normalized(title) in {normalized(line) for line in labels}:
                anchors[item['snapshot'], item['section']] = title
        located = []
        for (snapshot, section_id), title in anchors.items():
            aliases = [item['alias'] for item in self.aliases.values()
                       if item['snapshot'] == snapshot and item['coverage'] == 'mapped'
                       and any(s['section_id'] == section_id for s in self.lineage(item)) and self.writing_body(item)]
            require(aliases, f'no mapped original body in the registered section {title} ({section_id})')
            located.append({'snapshot_id': snapshot, 'section_id': section_id, 'aliases': aliases})
        return located

    def navigation_titles(self, read, navigation):
        """Expose selected headings along the reading's registered section paths."""
        lineage = {(self.aliases[alias]['snapshot'], section['section_id'])
                   for alias in read for section in self.lineage(self.aliases[alias])}
        return list(dict.fromkeys(self.label(alias)[1] for alias in navigation
                                 if (self.aliases[alias]['snapshot'], self.aliases[alias]['section']) in lineage))

    def explicit_dependencies(self, owned):
        materials = []
        for key in self.aliases:
            if key not in owned:
                continue
            item = self.aliases[key]
            text = self.read(item)
            for path in re.findall(r'\]\(<?(?:\./)?((?:tables|images)/[^\s)>]+)>?\)', text):
                require(unquote(path) in self.asset_paths[item['snapshot']],
                        f'{key} refers to an unregistered required asset: {path}')
            materials.append({'role': 'core', 'source_ref': item['ref'], 'unit_content_sha256': item['sha256'],
                              'core_text': text if item['locator']['kind'] == 'text' or text else None})
        _, supplement = required_reading(self.source, {'materials': materials, 'package_id': fingerprint(sorted(owned)),
            'task_id': 'incremental-writing-support', 'actor': self.access['actor'],
            'document_registry_revision': self.access['registry_revision']})
        links = []
        for link in supplement['links']:
            parent = self.units.get(canonical_json(link['parent']['unit_ref']))
            asset = self.units.get(canonical_json(link['dependency']['unit_ref']))
            require(parent and asset and parent['snapshot'] == asset['snapshot'] and asset['coverage'] != 'held',
                    'required registered asset lacks usable frozen source coverage')
            links.append((parent['alias'], asset['alias']))
        return links

    def writing_support(self, plan, *, writing_revision=CURRENT_WRITING_REVISION):
        """Bind writing to substantive support and its specific Map dependencies."""
        require(type(writing_revision) is int and writing_revision in (1, CURRENT_WRITING_REVISION),
                'unsupported incremental writing revision')
        has_body = self.body if writing_revision == 1 else self.writing_body

        def navigation_only(item):
            if writing_revision == 1:
                return self.label(item['alias'])[0] == 'navigation'
            return item['locator']['kind'] == 'text' and not has_body(item)

        selected = [self.units.get(canonical_json(ref['unit_ref'])) for ref in plan['added_support_refs']]
        require(all(selected) and all(ref['span'] is None for ref in plan['added_support_refs']),
                'new page support must resolve to complete units in the frozen Maps')
        navigation = {item['alias'] for item in selected if navigation_only(item)}
        scope = None
        located = []
        if not any(has_body(item) for item in selected):
            if writing_revision != 1:
                located = self.section_support(selected)
            if not located and (writing_revision == 1 or navigation):
                scope = self.chapter_support(plan['name'], selected, has_body)
                names = [item['alias'] if writing_revision == 1 else
                         f"{item['alias']} ({self.label(item['alias'])[1]})" for item in selected]
                require(scope, 'substantive page lacks uniquely located source body and clause evidence for '
                        + ', '.join(names))
        owned = {item['alias'] for item in selected if item['alias'] not in navigation}
        if scope:
            owned.update(scope['aliases'])
        owned.update(alias for section in located for alias in section['aliases'])
        edges, cross_references, relations = set(), set(), []
        for map_id, _, tid, topic in self.topics:
            if scope and (map_id, tid) != (scope['map_result_id'], scope['topic_id']):
                continue
            if any(r['item'] in owned or owned.intersection(r['applies_to']) for r in topic['relations']):
                relations.extend(topic['relations'])
        while True:
            prior = set(owned)
            for relation in relations:
                alias, role, parents = (relation[key] for key in ('item', 'role', 'applies_to'))
                if role == 'context':
                    continue
                for target in parents:
                    if alias == target or (alias not in owned and (role not in QUALIFIERS or target not in owned)):
                        continue
                    if alias in navigation or target in navigation:
                        continue
                    context_only = {key for key in (alias, target) if navigation_only(self.aliases[key])}
                    if context_only:
                        if writing_revision != 1:
                            navigation.update(context_only)
                        continue
                    require(all(self.aliases[key]['coverage'] == 'mapped' for key in (alias, target)),
                            f'clause dependency {alias} / {target} lacks mapped source coverage')
                    owned.update((alias, target))
                    left, right = self.aliases[alias], self.aliases[target]
                    if left['section'] == right['section'] or right['locator']['kind'] == 'asset':
                        edges.add((alias, target))
                    else:
                        # A mapped clause cross-reference names another requirement.
                        # Its separate writing owner retains the target's coverage.
                        cross_references.add((alias, target))
                    if role in QUALIFIERS:
                        edges.add((target, alias))
            if prior == owned:
                break
        require(owned and all(self.aliases[key]['coverage'] == 'mapped' for key in owned),
                'writing support includes a held or excluded source unit')
        explicit = self.explicit_dependencies(owned)
        for parent, asset in explicit:
            owned.add(asset)
            edges.add((parent, asset))
        refs = unique_refs(self.aliases[key]['ref'] for key in self.aliases if key in owned)
        change = {**plan, 'added_support_refs': [ref for ref in refs if ref not in plan['retained_support_refs']]}
        change.pop('change_id')
        audit = {'contract': 'incremental-writing-support/v1', 'page_plan_id': plan['change_id'],
                 'chapter': scope, 'navigation_aliases': sorted(navigation),
                 'selected_aliases': [item['alias'] for item in selected],
                 'qualified_aliases': [key for key in self.aliases if key in owned],
                 'required_asset_links': sorted([parent, asset] for parent, asset in explicit),
                 'clause_cross_references': sorted([*edge] for edge in cross_references),
                 'dependencies': sorted([*edge] for edge in edges)}
        if writing_revision != 1:
            audit.update(writing_revision=writing_revision, located_sections=located)
        change['writing_support_sha256'] = fingerprint(audit)
        change['change_id'] = fingerprint(change)
        return change, audit
