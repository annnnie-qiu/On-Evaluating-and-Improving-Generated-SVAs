"""Resolve existing Step A project settings without executing its pipeline."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from aq_source_parser import mask_comments_and_strings


def values(value):
    return value if isinstance(value, list) else [value] if value else []


def match_unit(config, source):
    mapping = config.get('units', {}).get('mapping', {})
    if source in mapping:
        return dict(mapping[source])
    return dict(next((u for pattern, u in mapping.items()
                      if fnmatch.fnmatch(source, pattern)), {}))


def project_name(path, root):
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def expand_files(specs, root):
    result = []
    for spec in values(specs):
        path = Path(spec)
        matches = sorted(root.glob(spec)) if any(c in spec for c in '*?[') else [root / path]
        if not matches:
            raise ValueError(f'RTL pattern matches no files: {spec}')
        for path in matches:
            name = project_name(path, root)
            if name not in result:
                result.append(name)
    return result


def infer_formal(source, config, unit):
    # Same explicit SVA clock / disable-iff rule used by Step A.
    formal = dict(config.get('formal', {}), **{k: unit[k] for k in
                  ('clock_mode', 'clock', 'reset_expression') if k in unit})
    masked, _ = mask_comments_and_strings(source)
    clocks = {m.group(2) for m in re.finditer(
        r'@\s*\(\s*(posedge|negedge)\s+([A-Za-z_$][A-Za-z0-9_$.]*)', masked)}
    resets = [source[m.start(1):m.end(1)].strip()
              for m in re.finditer(r'\bdisable\s+iff\s*\(([^()]*)\)', masked, re.S)]
    clock = formal.get('clock') or (next(iter(clocks)) if len(clocks) == 1 else '')
    reset = formal.get('reset_expression') or (
        resets[0] if resets and all(r == resets[0] for r in resets) else '')
    mode = formal.get('clock_mode', 'auto').lower()
    if mode == 'auto':
        if len(clocks) > 1:
            raise ValueError('multiple SVA clocks; configure clock_mode and clock explicitly')
        mode = 'clocked' if clock else 'none'
    if mode not in {'none', 'clocked'} or (mode == 'clocked' and not clock):
        raise ValueError('invalid clock_mode or missing clock for a clocked unit')
    return {'clock_mode': mode, 'clock': clock, 'reset_expression': reset}


def resolve_settings(root, config, source, inventory):
    unit = match_unit(config, source)
    paths = config.get('paths', {})
    rtl_cfg = config.get('rtl', {})
    units_cfg = config.get('units', {})
    source_path = root / paths.get('assertions_dir', 'assertions') / source
    if not source_path.is_file():
        source_path = root / 'buggy_assertions' / source
    text = source_path.read_text() if source_path.is_file() else ''
    masked, _ = mask_comments_and_strings(text)
    module = re.search(r'\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)', masked)
    guessed = module.group(1) if module else ''
    for suffix in values(units_cfg.get('infer_top_suffixes',
                                      ['_assertions', '_assertion', '_sva', '_properties'])):
        if suffix and guessed.endswith(suffix):
            guessed = guessed[:-len(suffix)]
            break
    top = unit.get('top') or units_cfg.get('default_top') or inventory.get('top') or guessed
    if not top:
        raise ValueError(f'{source}: cannot resolve top from mapping, Step A inventory or source')
    filelist = unit.get('rtl_filelist', rtl_cfg.get('filelist'))
    if filelist:
        raise ValueError(f'{source}: Step A uses RTL filelist {filelist}; provide its ordered '
                         'rtl_files in a separate Step B --aq-config instead of silently changing inputs')
    specs = unit.get('rtl_files', rtl_cfg.get('files', []))
    automatic = not bool(specs)
    rtl_files = expand_files(specs, root)
    if not rtl_files and rtl_cfg.get('auto_discover', True):
        directory = root / paths.get('rtl_dir', 'rtl')
        allowed = {x.lower() for x in rtl_cfg.get('extensions', ['.svh', '.vh', '.sv', '.v'])}
        candidates = [p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in allowed]
        candidates.sort(key=lambda p: (0 if p.suffix.lower() in {'.svh', '.vh'} else 1,
                                       0 if p.stem.endswith('_pkg') else 1, p.as_posix()))
        rtl_files = [project_name(p, root) for p in candidates]
    if not rtl_files:
        raise ValueError(f'{source}: no RTL files resolved')
    mode = unit.get('clock_mode', config.get('formal', {}).get('clock_mode', 'auto'))
    if mode == 'auto' and not text:
        raise ValueError(f'{source}: assertion source required for automatic clock resolution')
    formal = infer_formal(text, config, unit)
    bind_cfg = config.get('bind', {})
    binds = unit.get('bind_files', [])
    if not binds:
        binds = next((v for pat, v in bind_cfg.get('mapping', {}).items()
                      if fnmatch.fnmatch(source, pat)), [])
    if not binds and bind_cfg.get('mode', 'existing_auto') in {'existing_auto', 'mapping'}:
        directory = root / paths.get('bind_dir', 'bind')
        stem = Path(source).stem
        patterns = bind_cfg.get('auto_patterns',
            [f'**/{stem}*bind*.sv', f'**/*{stem}*.sv', f'**/*{stem}*.svh'])
        binds = sorted({project_name(p, root) for pat in patterns
                        for p in directory.glob(pat.replace('{stem}', stem)) if p.is_file()})
    if not binds and bind_cfg.get('mode') == 'template':
        raise ValueError(f'{source}: configure explicit saved bind_files for Step B')
    target = unit.get('mutation_target')
    if not target and unit.get('rtl_files'):
        # Preserve the established explicit-mapping default and old cache identity.
        target = rtl_files[-1]
    if not target:
        matches = []
        for filename in rtl_files:
            path = root / filename
            if path.is_file():
                rtl_text, _ = mask_comments_and_strings(path.read_text(errors='replace'))
                if re.search(r'\bmodule\s+(?:(?:automatic|static)\s+)?' + re.escape(top) + r'\b', rtl_text):
                    matches.append(filename)
        if len(matches) != 1:
            raise ValueError(f'{source}: mutation target for top {top} is ambiguous/missing: {matches}; '
                             'configure mutation_target explicitly')
        target = matches[0]
    if target not in rtl_files:
        raise ValueError(f'{source}: mutation_target is not in the resolved RTL file list')
    settings = dict(top=top, rtl_files=rtl_files, bind_files=values(binds),
        include_dirs=values(unit.get('include_dirs', rtl_cfg.get('include_dirs',
                            [paths.get('rtl_dir', 'rtl')]))),
        defines=[str(x) for x in values(unit.get('defines', rtl_cfg.get('defines', [])))],
        **formal)
    evidence = {'assertion_source': str(source_path), 'top': top, 'mutation_target': target,
                'rtl_resolution': 'Step A automatic discovery' if automatic else 'explicit RTL list',
                'formal_environment': formal, 'external_bind_files': settings['bind_files'],
                'note': 'bind.mode=none is preserved; embedded binds remain in the assertion source'}
    return settings, target, evidence
