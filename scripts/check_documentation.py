"""Check local Markdown links in the operator-facing handoff without network access."""
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    files = [*root.glob('*.md'), *root.glob('docs/*.md'),
             *root.glob('examples/*.md'), *root.glob('packages/*.md')]
    links = 0
    failures = []
    for file in files:
        text = re.sub(r'```.*?```', '', file.read_text(encoding='utf-8'), flags=re.S)
        for raw in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            target = raw.strip('<>').split('#')[0]
            if not target or re.match(r'^[a-zA-Z]+:', target):
                continue
            links += 1
            resolved = (file.parent / unquote(target)).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                failures.append(file.relative_to(root).as_posix() + ': ' + target)
    if failures:
        raise ValueError('Broken or escaping links: ' + '; '.join(failures))
    return {'markdown_files': len(files), 'local_links_checked': links, 'result': 'passed'}


if __name__ == '__main__':
    print(check())
