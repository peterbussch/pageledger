"""Explicit document processing policy, separate from rerun adapter generations."""
from __future__ import annotations

import math
from typing import Any

STAGES = ('local_text', 'local_ocr', 'image', 'second_opinion')
PROMPT = ('Transcribe only text visible on this source page, preserving page order, headings, '
          'footnotes and named table columns. Do not reconstruct missing or invisible text. '
          'Mark unreadable spans explicitly. Return the transcript without interpretation.')


def processing_config(data: dict[str, Any], *, pdf: bool) -> dict[str, Any]:
    value = data.get('processing', {})
    if not isinstance(value, dict):
        raise ValueError('processing must be a mapping')
    unknown = set(value) - {*STAGES, 'limits', 'links'}
    if unknown:
        raise ValueError(f'Unknown processing key: {sorted(unknown)[0]}')
    result: dict[str, Any] = {}
    for name in STAGES:
        default = ({'adapter': 'pdf_text' if pdf else 'text'} if name == 'local_text'
                   else {'adapter': 'pdf_ocr'} if name == 'local_ocr' and pdf else None)
        entry = value.get(name, default)
        if entry is None:
            if name == 'local_text':
                raise ValueError('processing.local_text is required')
            result[name] = None
            continue
        if not isinstance(entry, dict) or set(entry) - {'adapter', 'adapter_options', 'prompt'}:
            raise ValueError(f'Invalid processing.{name} profile')
        adapter = entry.get('adapter')
        options = entry.get('adapter_options', {})
        prompt = entry.get('prompt', PROMPT)
        if (not isinstance(adapter, str) or not adapter.strip()
                or not isinstance(options, dict) or not all(isinstance(k, str) for k in options)
                or not isinstance(prompt, str) or not prompt.strip()):
            raise ValueError(f'Invalid processing.{name} adapter, options or prompt')
        if 'evidence_dir' in options:
            raise ValueError('processing owns the adapter evidence_dir')
        result[name] = {'adapter': adapter, 'adapter_options': dict(options), 'prompt': prompt}
    if result['second_opinion'] is not None and result['image'] is None:
        raise ValueError('second_opinion requires an image stage')
    if result['image'] is not None and result['local_ocr'] is None:
        raise ValueError('An image stage requires an explicit local_ocr stage')
    limits = value.get('limits', {})
    keys = {'max_attempt_pages', 'max_image_pages', 'max_tokens', 'max_cost_usd'}
    if not isinstance(limits, dict) or set(limits) - keys:
        raise ValueError('Invalid processing.limits')
    normalized: dict[str, Any] = {key: limits.get(key) for key in keys}
    normalized['max_image_pages'] = limits.get('max_image_pages', 0)
    for key, number in normalized.items():
        if number is None and key != 'max_image_pages':
            continue
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number < 0
                or (key != 'max_cost_usd' and not isinstance(number, int))):
            raise ValueError(f'processing.limits.{key} must be a nonnegative number')
    if result['image'] is not None and normalized['max_image_pages'] < 1:
        raise ValueError('An image stage requires a positive max_image_pages bound')
    result['limits'] = normalized
    links = value.get('links', {})
    if not isinstance(links, dict) or set(links) - {'article', 'custody'}:
        raise ValueError('processing.links accepts article and custody references only')
    if any(not isinstance(v, str) or not v for v in links.values()):
        raise ValueError('processing.links values must be nonempty strings')
    result['links'] = {'article': links.get('article'), 'custody': links.get('custody')}
    return result
