"""Release policy for the selected USB tree, independent of genre inference."""
import re

def verify_bucket_coverage(xml, selected):
    selected = set(map(str, selected)); covered = set(); errors = []; nodes = 0
    def walk(node, path=()):
        nonlocal nodes
        name = node.attrib.get('Name', '')
        current = path if name == 'ROOT' else path + (name,)
        if name != 'ROOT': nodes += 1
        if node.attrib.get('Type') == '1':
            ids = [t.attrib['Key'] for t in node.findall('./TRACK')]
            if len(ids) > 1000: errors.append('playlist_above_1000:' + '/'.join(current))
            if len(current) == 4 and current[:2] == ('USB', 'DJ Bacs') and re.fullmatch(r'Tous(?: \(\d+\))?', current[-1]):
                covered.update(ids)
        for child in node.findall('./NODE'): walk(child, current)
    walk(xml.find('./PLAYLISTS/NODE'))
    if nodes > 1000: errors.append('usb_elements_above_1000')
    if not selected or covered != selected: errors.append('usb_dj_bucket_coverage_mismatch')
    return errors
