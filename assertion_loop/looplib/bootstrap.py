"""Reuse fixed harnesses when present; conservative ANSI-module fallback."""
from __future__ import annotations
import fnmatch
import re
import sys
from pathlib import Path
from .common import ROOT, read, save, resolve, EnvironmentSetupError

sys.path.insert(0, str(ROOT / "vendor" / "aq"))
import assertion_aq_pipeline as aq

MARKER = "/* LOOP_ASSERTIONS */"

def module_header(source, module):
    masked, errors = aq.mask_comments_and_strings(source)
    if errors:
        raise ValueError(f"Cannot parse module {module}: {errors}")
    m = re.search(r"\bmodule\s+" + re.escape(module) + r"\b", masked)
    if not m:
        raise ValueError(f"No module {module}")
    depth = 0
    for i in range(m.end(), len(masked)):
        c = masked[i]
        if c == "(": depth += 1
        if c == ")": depth -= 1
        if c == ";" and depth == 0:
            return source[m.start():i+1]
    raise ValueError(f"Incomplete module header: {module}")

def stripped_template(source):
    blocks, errors = aq.extract_assertions(source)
    if errors:
        raise ValueError(f"Unsafe existing checker: {errors}")
    result = aq.render_subset(source, blocks, set())
    matches = list(re.finditer(r"\bendmodule\b", aq.mask_comments_and_strings(result)[0]))
    if len(matches) != 1:
        raise ValueError("Use a fixed template with exactly one checker module")
    i = matches[0].start()
    return result[:i] + MARKER + "\n" + result[i:]

def fallback_template(root, top, checker, rtl):
    source = next((p.read_text() for p in rtl if re.search(r"\bmodule\s+"+re.escape(top)+r"\b", p.read_text())), None)
    if source is None:
        raise ValueError(f"Cannot locate RTL top {top}")
    header = module_header(source, top)
    # ANSI only. For old non-ANSI/internal checker ports provide an explicit fixed template.
    if not re.search(r"\b(input|output|inout)\b", header):
        raise ValueError(f"{top}: non-ANSI interface requires a fixed checker template in gen_assertions/setup/")
    header = re.sub(r"\bmodule\s+"+re.escape(top)+r"\b", "module "+checker, header, count=1)
    # Checker ports are observations. Preserve widths, parameters and port names.
    header = re.sub(r"\b(output|inout)\b", "input", header)
    return header + "\n" + MARKER + "\nendmodule\n"

def target_scope(root,unit):
    """Use the configured bind instance, even when the template also has a bind.

    No RTL, bind, helper logic or assumption is changed. No first-match fallback.
    """
    explicit=unit.get("target_scope")
    if explicit is not None:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+",explicit):
            raise EnvironmentSetupError(f"{unit['name']}: invalid target_scope: {explicit!r}")
        return explicit
    matches=[]
    for filename in unit.get("bind_files",[]):
        clean,errors=aq.mask_comments_and_strings(resolve(root,filename).read_text())
        if errors: raise EnvironmentSetupError(f"Cannot parse fixed bind {filename}: {errors}")
        for bind in re.finditer(r"\bbind\s+(\w+)\s+(\w+)\b",clean):
            top,checker=bind.groups()
            if top!=unit["top"] or checker!=unit["checker"]: continue
            i=bind.end()
            while i<len(clean) and clean[i].isspace(): i+=1
            if i<len(clean) and clean[i]=="#":
                i+=1
                while i<len(clean) and clean[i].isspace(): i+=1
                if i>=len(clean) or clean[i]!="(":
                    raise EnvironmentSetupError(f"Cannot parse bind parameters in {filename}")
                depth=1; i+=1
                while i<len(clean) and depth:
                    if clean[i]=="(": depth+=1
                    elif clean[i]==")": depth-=1
                    i+=1
                if depth: raise EnvironmentSetupError(f"Unclosed bind parameters in {filename}")
            instance=re.match(r"\s*([A-Za-z_]\w*)\s*\(",clean[i:])
            if not instance:
                raise EnvironmentSetupError(f"Cannot resolve checker instance in {filename}; set target_scope explicitly")
            matches.append(f"{top}.{instance[1]}")
    if len(matches)!=1:
        raise EnvironmentSetupError(f"{unit['name']}: expected one configured checker bind, found {matches}; set target_scope explicitly")
    return matches[0]


def init_dataset(root):
    from .inputs import SOURCE_SUFFIXES
    root = Path(root).resolve()
    setup = root / "gen_assertions" / "setup"
    config_path = setup / "dataset.json"
    if config_path.exists():
        return validate_setup(root, read(config_path))
    for name in ("rtl", "spec", "bind"):
        if not (root / name).is_dir():
            raise ValueError(f"Dataset needs {name}/: {root}")
    rtl = sorted(p for p in (root / "rtl").rglob("*") if p.suffix in {".sv", ".v"})
    specs = sorted(p for p in (root / "spec").rglob("*") if p.suffix.lower() in {".txt", ".md", ".rst"})
    if not rtl or not specs:
        raise ValueError("Need RTL sources and text specifications (.md/.txt/.rst). Export PDF/DOCX specs to text first.")
    old = {}
    old_config = root / "aq_qualification_pipeline_v1" / "aq_config.json"
    if old_config.is_file(): old = read(old_config)
    mapping = old.get("units", {}).get("mapping", {})
    units, seen = [], set()
    for bind in sorted((root / "bind").rglob("*.sv")):
        clean = aq.mask_comments_and_strings(bind.read_text())[0]
        bindings = re.findall(r"\bbind\s+(\w+)\s+(\w+)\b", clean)
        if len(bindings) != 1:
            raise ValueError(f"{bind}: expected one simple bind per file; supply gen_assertions/setup/dataset.json explicitly")
        top, checker = bindings[0]
        if top in seen:
            raise ValueError(f"Multiple bind checkers for {top}; configure explicit units")
        seen.add(top)
        existing = next((p for p in (root / "assertions").glob("**/*")
                         if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
                         and re.search(r"\bmodule\s+"+re.escape(checker)+r"\b", p.read_text())), None)
        config = next((v for k,v in mapping.items() if v.get("top") == top), {})
        paths = config.get("rtl_files", old.get("rtl", {}).get("files", []))
        if not paths:
            # All RTL is a best-effort compile list, not a claimed inferred hierarchy.
            paths = [str(p.relative_to(root)) for p in sorted(rtl, key=lambda p:(not p.stem.endswith("_pkg"), str(p)))]
        template = stripped_template(existing.read_text()) if existing else fallback_template(root, top, checker, rtl)
        template_path = setup / "templates" / f"{top}.sv"
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(template)
        clock, reset, unique = aq.infer_clock_and_reset(existing.read_text() if existing else "\n".join(p.read_text() for p in rtl if re.search(r"\bmodule\s+"+re.escape(top)+r"\b", p.read_text())))
        mode = config.get("clock_mode", "clocked" if clock else "none")
        units.append({"name":top, "top":top, "checker":checker,
                      "template":str(template_path.relative_to(root)), "rtl_files":paths,
                      "bind_files":[str(bind.relative_to(root))], "environment_files":[],
                      "include_dirs":config.get("include_dirs", old.get("rtl",{}).get("include_dirs",["rtl"])),
                      "defines":config.get("defines",old.get("rtl",{}).get("defines",[])),
                      "clock_mode":mode, "clock":config.get("clock",clock),
                      "reset_expression":config.get("reset_expression",reset),
                      "spec_files":[str(p.relative_to(root)) for p in specs],
                      "mutation_targets":[p for p in paths if any(re.search(r"\bmodule\s+"+re.escape(top)+r"\b", resolve(root,p).read_text()) for _ in [0])],
                      "pure_functions":[], "fixed_dependencies":[],
                      "setup_note":"Reused fixed checker" if existing else "Auto ANSI checker; review compile order, clock/reset and bind port compatibility"})
    if not units:
        raise ValueError("No simple binds found. Add explicit unit templates/config; see examples/dataset.json")
    result = {"version":1, "units":units}
    save(config_path, result)
    return validate_setup(root,result)

def validate_setup(root, setup):
    names = set()
    if not setup.get("units"): raise ValueError("No units configured")
    for u in setup["units"]:
        name = u["name"]
        if not re.fullmatch(r"[A-Za-z_]\w*",name) or name in names:
            raise ValueError(f"Unsafe/duplicate unit: {name}")
        names.add(name)
        template = resolve(root,u["template"]).read_text()
        if template.count(MARKER) != 1 or aq.detected_assertion_count(template):
            raise ValueError(f"{name}: fixed template needs one {MARKER} and no target assertions")
        if u.get("clock_mode") not in {"clocked","none"}:
            raise ValueError(f"{name}: explicitly set clock_mode")
        if u["clock_mode"] == "clocked" and not u.get("clock"):
            raise ValueError(f"{name}: clock not resolved; edit gen_assertions/setup/dataset.json")
        if not u.get("mutation_targets") or not set(u["mutation_targets"]) <= set(u["rtl_files"]):
            raise ValueError(f"{name}: mutation_targets must be nonempty members of ordered rtl_files")
        target_scope(root,u)
        for key in ("rtl_files","bind_files","environment_files","include_dirs","spec_files"):
            for p in u.get(key,[]):
                if not resolve(root,p).exists(): raise ValueError(f"Missing {key}: {p}")
    return setup
