"""Read-only source inventory; annotation presence is separate from extracted body text."""
from __future__ import annotations

from pathlib import Path


def inspect_source(source: Path) -> tuple[int, dict]:
    if source.suffix.lower() != '.pdf':
        text = source.read_text(encoding='utf-8')
        return text.count('\f') + 1, {'status': 'none', 'count': 0}
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError('PDF processing requires the pdf extra') from exc
    with source.open('rb') as stream:
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted:
            raise ValueError('Encrypted source container requires an accessible source')
        seen = set()
        def count_tree(reference):
            node = reference.get_object()
            identity = id(node)
            if identity in seen:
                raise ValueError('Repeated or cyclic PDF page-tree node')
            seen.add(identity)
            if node.get('/Type') == '/Page':
                return 1
            if node.get('/Type') != '/Pages' or '/Kids' not in node:
                raise ValueError('Invalid PDF page tree')
            count = sum(count_tree(child) for child in node['/Kids'])
            declared = node.get('/Count')
            if not isinstance(declared, int) or declared != count:
                raise ValueError('Declared PDF page count differs from actual page inventory')
            return count
        catalog = reader.trailer['/Root']
        if not isinstance(catalog, dict) or '/Pages' not in catalog:
            raise ValueError('Invalid PDF catalog')
        count = count_tree(catalog['/Pages'])
        if count < 1 or len(reader.pages) != count:
            raise ValueError('PDF page inventory mismatch')
        annotations = 0
        for page in reader.pages:
            values = page.get('/Annots')
            if values is not None:
                values = values.get_object()
                if not isinstance(values, list):
                    raise ValueError('Invalid PDF annotation inventory')
                annotations += len(values)
        return count, {'status': 'present' if annotations else 'none', 'count': annotations}
