"""Independent observable-difference checks. Never uses generated assertions.

Both designs receive the same primary inputs. The original fixed harness and
its assumptions are attached to the reference only. Matching uninitialized
registers are related at time zero, which aligns initial states without
restricting either design's input domain. Unsupported mappings are unresolved.
"""
from __future__ import annotations
import re
from pathlib import Path
from .common import digest, read, save, resolve
from .jasper_jobs import JasperAttempt, error_details
from .properties import render

VERSION = "observable_equivalence_v1"
CALIBRATION_METHOD = "proved_state_relation_induction_v1"

class UnsupportedEquivalence(ValueError):
    pass

def slang():
    try:
        import pyslang
        return pyslang
    except ImportError as error:
        raise RuntimeError("Fault qualification requires pyslang; install requirements.txt") from error

def errors(compilation):
    return [str(d.code) for d in compilation.getAllDiagnostics() if d.isError()]

def expanded(files, include_dirs=(), defines=()):
    p=slang(); pp=p.parsing.PreprocessorOptions()
    pp.additionalIncludePaths=[str(x) for x in include_dirs]
    pp.predefines=list(defines)
    bag=p.Bag();bag.preprocessorOptions=pp
    tree=p.syntax.SyntaxTree.fromFiles([str(x) for x in files],p.SourceManager(),bag)
    if any(d.isError() for d in tree.diagnostics):
        raise UnsupportedEquivalence("Preprocessing failed: "+", ".join(str(d.code) for d in tree.diagnostics))
    return str(tree.root)

def reachable_source(source,top):
    """Keep configured top and its syntactic module dependencies.

    Dataset file lists often contain unrelated tops importing absent external
    packages. They are not part of this unit's elaborated design. Packages stay
    shared; generate branches within reachable modules are left to elaboration.
    """
    p=slang();tree=p.syntax.SyntaxTree.fromText(source);modules={}
    def collect(n):
        if str(n.kind).split('.')[-1]=='ModuleDeclaration':modules[n.header.name.rawText]=n
    tree.root.visit(collect)
    if top not in modules:raise UnsupportedEquivalence('Configured top declaration missing: '+top)
    active=set();todo=[top]
    while todo:
        name=todo.pop()
        if name in active:continue
        active.add(name)
        def child(n):
            if str(n.kind).split('.')[-1]=='HierarchyInstantiation' and n.type.rawText in modules and n.type.rawText not in active:todo.append(n.type.rawText)
        modules[name].visit(child)
    edits=[(n.sourceRange.start.offset,n.sourceRange.end.offset) for name,n in modules.items() if name not in active]
    for a,b in sorted(edits,reverse=True):source=source[:a]+source[b:]
    return source


def design_info(source,top):
    p=slang(); opts=p.ast.CompilationOptions();opts.topModules={top}
    bag=p.Bag();bag.compilationOptions=opts
    c=p.ast.Compilation(bag);c.addSyntaxTree(p.syntax.SyntaxTree.fromText(source))
    root=c.getRoot();bad=errors(c)
    if bad:raise UnsupportedEquivalence("RTL elaboration: "+", ".join(bad[:12]))
    instances=[i for i in root.topInstances if i.name==top]
    if len(instances)!=1:raise UnsupportedEquivalence("Cannot resolve one configured top")
    inst=instances[0];ports=[];states={};initialized=set()
    for port in inst.body.portList:
        direction=str(getattr(port,"direction","")).split(".")[-1]
        width=getattr(getattr(port,"type",None),"bitWidth",0)
        if direction not in {"In","Out"} or not width or not re.fullmatch(r"[A-Za-z_]\w*",port.name):
            raise UnsupportedEquivalence("Only named packed input/output ports are supported: "+port.name)
        ports.append({"name":port.name,"direction":direction,"width":width,"type":str(port.type)})
        if direction=="Out" and getattr(port,"initializer",None) is not None:
            symbol=getattr(port,"internalSymbol",None)
            if symbol is not None and symbol.hierarchicalPath.startswith(top+"."):
                initialized.add(symbol.hierarchicalPath[len(top)+1:])
    def targets(expr):
        kind=str(expr.kind).split(".")[-1]
        if kind=="Concatenation":
            result=[]
            for item in expr.operands:result.extend(targets(item))
            return result
        symbol=expr.getSymbolReference()
        return [symbol] if symbol is not None else []
    def visit(node):
        kind=str(node.kind).split(".")[-1]
        if kind=="Assignment" and node.isNonBlocking:
            for symbol in targets(node.left):
                path=symbol.hierarchicalPath
                if not path.startswith(top+"."):raise UnsupportedEquivalence("External state reference")
                relative=path[len(top)+1:]
                if not re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*",relative):
                    raise UnsupportedEquivalence("Unsupported state path: "+relative)
                width=getattr(symbol.type,"bitWidth",0)
                if not width:raise UnsupportedEquivalence("Unpacked/nonintegral state: "+relative)
                states[relative]={"type":str(symbol.type),"width":width}
                if getattr(symbol,"initializer",None) is not None:initialized.add(relative)
        if kind=="ProceduralBlock" and str(node.procedureKind).split(".")[-1]=="AlwaysLatch":
            raise UnsupportedEquivalence("Latch initial-state mapping is not supported")
        if kind=="ProceduralBlock" and (str(node.procedureKind).split(".")[-1]=="AlwaysFF" or
                                       str(node.procedureKind).split(".")[-1]=="Always" and re.search(r"\b(?:posedge|negedge)\b",str(node.syntax))):
            def sequential(n):
                if str(n.kind).split(".")[-1]=="Assignment":
                    for symbol in targets(n.left):
                        path=symbol.hierarchicalPath
                        if not path.startswith(top+"."):raise UnsupportedEquivalence("External sequential state")
                        relative=path[len(top)+1:];width=getattr(symbol.type,"bitWidth",0)
                        if not width or not re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*",relative):raise UnsupportedEquivalence("Unsupported sequential state: "+relative)
                        states[relative]={"type":str(symbol.type),"width":width}
                        if getattr(symbol,"initializer",None) is not None:initialized.add(relative)
            node.body.visit(sequential)
        if kind=="ProceduralBlock" and str(node.procedureKind).split(".")[-1]=="Initial":
            def initial(n):
                if str(n.kind).split(".")[-1]=="Assignment":
                    for symbol in targets(n.left):
                        path=symbol.hierarchicalPath
                        if path.startswith(top+"."):initialized.add(path[len(top)+1:])
            node.body.visit(initial)
    inst.visit(visit)
    return {"ports":ports,"states":states,"initialized_states":sorted(initialized)}

def rewrite(source,names,suffix,*,reference=False,common=False):
    """Rewrite declaration/instantiation tokens after preprocessing, not regex RTL."""
    p=slang();tree=p.syntax.SyntaxTree.fromText(source);edits=[];binds=[];packages=[]
    def span(node):return node.sourceRange.start.offset,node.sourceRange.end.offset
    def edit_token(token,value):edits.append((token.location.offset,token.location.offset+len(token.rawText),value))
    def walk(node):
        kind=str(node.kind).split(".")[-1]
        if kind=="PackageDeclaration":
            a,b=span(node);packages.append(source[a:b]);edits.append((a,b,""));return
        if kind=="BindDirective":
            a,b=span(node);binds.append(source[a:b]);edits.append((a,b,""));return
        if kind=="ModuleDeclaration":
            name=node.header.name.rawText
            if name in names:edit_token(node.header.name,name+suffix)
        elif kind=="HierarchyInstantiation" and node.type.rawText in names:
            edit_token(node.type,node.type.rawText+suffix)
    tree.root.visit(walk)
    # Child edits inside a removed bind/package must not survive.
    clean=[]
    for e in sorted(set(edits),key=lambda e:(e[0],-e[1])):
        if clean and e[0]<clean[-1][1]:continue
        clean.append(e)
    for a,b,new in reversed(clean):source=source[:a]+new+source[b:]
    if reference:
        for binding in binds:
            bt=p.syntax.SyntaxTree.fromText(binding);changes=[]
            def bv(n):
                if str(n.kind).split(".")[-1]=="BindDirective":
                    old=str(n.target).strip()
                    if old not in names:raise UnsupportedEquivalence("Unsupported instance-specific bind target: "+old)
                    a,b=span(n.target);changes.append((a,b,old+suffix))
            bt.root.visit(bv)
            for a,b,new in reversed(changes):binding=binding[:a]+new+binding[b:]
            source+="\n"+binding
    return source,packages

def miter_source(reference,candidate,harness,unit,*,calibration=False):
    """Return source plus exact port/state/proof-target provenance."""
    reference=reachable_source(reference,unit['top']);candidate=reachable_source(candidate,unit['top'])
    if calibration and reference!=candidate:
        raise UnsupportedEquivalence('Golden calibration requires identical reference/candidate sources')
    p=slang();ri=design_info(reference,unit['top']);mi=design_info(candidate,unit['top'])
    if ri['ports']!=mi['ports']:
        return None,{"status":"INTERFACE_INCOMPATIBLE","reason":"Elaborated port name/direction/type/width differs","reference_ports":ri['ports'],"mutant_ports":mi['ports']}
    if ri['states']!=mi['states']:
        return None,{"status":"UNRESOLVED","reason":"State shape/path changed; no initial-state correspondence was invented","reference_states":ri['states'],"mutant_states":mi['states']}
    if ri['initialized_states']!=mi['initialized_states']:
        return None,{"status":"UNRESOLVED","reason":"Explicit initialization domains differ"}
    names=set()
    def names_visit(n):
        if str(n.kind).split('.')[-1]=='ModuleDeclaration':names.add(n.header.name.rawText)
    p.syntax.SyntaxTree.fromText(reference).root.visit(names_visit)
    mutant_names=set()
    p.syntax.SyntaxTree.fromText(candidate).root.visit(lambda n:mutant_names.add(n.header.name.rawText) if str(n.kind).split('.')[-1]=='ModuleDeclaration' else None)
    if names!=mutant_names:raise UnsupportedEquivalence('Module inventory changed')
    ref,packages=rewrite(reference,names,'__eq_ref',reference=True)
    mut,mutant_packages=rewrite(candidate,names,'__eq_mut')
    if packages!=mutant_packages:raise UnsupportedEquivalence('Mutated package requires separate type/package correspondence; not silently shared')
    fixed,fixed_packages=rewrite(harness,names,'__eq_ref',reference=True)
    # Mutant-local assumptions could hide the behavioral difference. Such RTL
    # is explicitly unresolved instead of silently deleting/changing constraints.
    internal_constraints=[]
    def constraint_node(node):
        kind=str(node.kind)
        if "Assume" in kind or "Restrict" in kind:internal_constraints.append(kind)
    p.syntax.SyntaxTree.fromText(mut).root.visit(constraint_node)
    if internal_constraints:raise UnsupportedEquivalence('RTL-internal assumptions/restrictions need an explicit reference-only environment mapping')
    inputs=[x for x in ri['ports'] if x['direction']=='In'];outputs=[x for x in ri['ports'] if x['direction']=='Out']
    if not outputs:raise UnsupportedEquivalence('No observable output ports')
    def decl(x):
        # Package enum/struct input types must survive the wrapper: connecting
        # raw logic to an enum is illegal and can alter its elaborated domain.
        if re.fullmatch(r"[A-Za-z_]\w*::[A-Za-z_]\w*",x["type"]):return x["type"]+" "+x["name"]
        return f"logic [{x['width']-1}:0] {x['name']}" if x['width']>1 else 'logic '+x['name']
    lines=['module EQ_MITER('+', '.join('input '+decl(x) for x in inputs)+');']
    for x in outputs:
        for side in ('ref','mut'):lines.append(decl({**x,'name':side+'_'+x['name']})+';')
    for side in ('ref','mut'):
        connections=', '.join('.'+x['name']+'('+(x['name'] if x['direction']=='In' else side+'_'+x['name'])+')' for x in ri['ports'])
        lines.append(unit['top']+'__eq_'+side+' '+side+'_dut('+connections+');')
    coupled=[]
    for state in sorted(set(ri['states'])-set(ri['initialized_states'])):
        coupled.append(state);lines.append(f'initial begin EQ_INIT_{len(coupled):04d}: assume (ref_dut.{state} == mut_dut.{state}); end')
    clocked=unit['clock_mode']=='clocked'
    if clocked and unit['clock'] not in {x['name'] for x in inputs}:raise UnsupportedEquivalence('Clock is not a primary input')
    event='@('+f"posedge {unit['clock']}"+')' if clocked else ''
    wrapper_reset=unit.get('reset_expression','')
    if clocked and wrapper_reset:
        from jasper_reset import reset_pins
        pins=reset_pins(wrapper_reset)
        primary={x['name'] for x in inputs}
        # The miter exposes primary inputs. A checker may observe an internal
        # combinational reset alias (e.g. rst_i); that alias belongs to ref_dut,
        # not to the newly generated wrapper's otherwise empty scope.
        if any(pin.lstrip('!') not in primary for pin in pins):
            wrapper_reset=' || '.join(('!' if pin.startswith('!') else '')+
                (pin.lstrip('!') if pin.lstrip('!') in primary else 'ref_dut.'+pin.lstrip('!')) for pin in pins)
    disable=f" disable iff ({wrapper_reset})" if clocked and wrapper_reset else ''
    def assertion(label,expr):
        return f'{label}: assert property ({event}{disable} ({expr}));' if clocked else f'always_comb begin {label}: assert ({expr}); end'
    labels={f'EQ_OUTPUT_{i:04d}':x['name'] for i,x in enumerate(outputs,1)}
    lemmas={}
    if calibration and clocked and ri['states']:
        # Calibration alone: prove a complete state relation by induction. The
        # old unconditional 1600-bit output invariant required Jasper to invent
        # missing control-register invariants in the SHA3 sequential miter.
        # These are ASSERTIONS, not assume-guarantee shortcuts: every obligation
        # must be fully PROVEN before conditional output results can be used.
        reserved={'EQ_CAL_STATE_EQUAL','EQ_CAL_SEEN','EQ_CAL_BASE','EQ_CAL_STEP'}
        if reserved & {x['name'] for x in inputs}:
            raise UnsupportedEquivalence('Calibration helper name collides with an input port')
        relation=' && '.join(f'(ref_dut.{s} == mut_dut.{s})' for s in sorted(ri['states']))
        lines.append('wire EQ_CAL_STATE_EQUAL = '+relation+';')
        lines.append("logic EQ_CAL_SEEN = 1'b0;")
        # Reset history as well as initializing it. Otherwise a Jasper reset
        # prefix could set SEEN before proof starts and vacuously skip the base.
        history_reset=f"if ({wrapper_reset}) EQ_CAL_SEEN <= 1'b0; else " if wrapper_reset else ''
        lines.append(f"always @(posedge {unit['clock']}) {history_reset}EQ_CAL_SEEN <= 1'b1;")
        lines.append(assertion('EQ_CAL_BASE','!EQ_CAL_SEEN |-> EQ_CAL_STATE_EQUAL'))
        # Deliberately no disable iff: reset cannot cancel a preservation check.
        lines.append(f'EQ_CAL_STEP: assert property ({event} (EQ_CAL_STATE_EQUAL |=> EQ_CAL_STATE_EQUAL));')
        lines.extend(assertion(label,f'EQ_CAL_STATE_EQUAL |-> (ref_{name} == mut_{name})') for label,name in labels.items())
        lemmas={'EQ_CAL_BASE':'complete state relation at first enabled sample after initialization/reset',
                'EQ_CAL_STEP':'complete state relation is preserved at the next sample, including reset transitions'}
    else:
        lines.extend(assertion(label,f'ref_{name} == mut_{name}') for label,name in labels.items())
    lines.append(assertion('EQ_ENV_WITNESS',"1'b0"));lines.append('endmodule')
    source='\n'.join(list(dict.fromkeys(packages+fixed_packages))+[ref,mut,fixed]+lines)
    # Check the exact emitted SV, including binds, in the real compiler front end.
    opts=p.ast.CompilationOptions();opts.topModules={'EQ_MITER'};bag=p.Bag();bag.compilationOptions=opts
    c=p.ast.Compilation(bag);c.addSyntaxTree(p.syntax.SyntaxTree.fromText(source));c.getRoot()
    bad=errors(c)
    if bad:raise UnsupportedEquivalence('Generated miter elaboration: '+', '.join(bad[:12]))
    info={"status":"PREPARED","outputs":labels,"coupled_initial_registers":coupled,
        "initialization_policy":"same unconstrained initial values for matching registers; explicit initializers remain independent",
        "environment_policy":"original fixed harness on reference only; no generated assertion used"}
    if wrapper_reset!=unit.get('reset_expression',''):
        info['formal_reset_expression']=wrapper_reset
    if calibration:
        info.update(calibration_method=CALIBRATION_METHOD if lemmas else 'direct_output_comparison',
                    calibration_lemmas=lemmas,
                    conditional_outputs=bool(lemmas),state_relation_registers=sorted(ri['states']))
    return source,info

def classify_calibration(statuses,info):
    """Conditional outputs alone never establish equivalence.

    BASE establishes R at the first enabled sample; STEP proves R(t)=>R(t+1)
    without reset cancellation. Thus R holds at every enabled sample, and each
    proven R=>output-equality establishes the original output property. The
    environment witness is still required. No bounded/unknown result qualifies.
    """
    required=[*info['outputs'],*info.get('calibration_lemmas',{})]
    if statuses.get('EQ_ENV_WITNESS')=='CEX' and all(statuses.get(label)=='PROVEN' for label in required):
        return 'EQUIVALENT',[]
    return 'UNRESOLVED',[]

def classify(statuses,outputs):
    affected=[name for label,name in outputs.items() if statuses.get(label)=='CEX']
    if affected:return 'DIFFERENT',affected
    if statuses.get('EQ_ENV_WITNESS')!='CEX':return 'UNRESOLVED',[]
    if all(statuses.get(label)=='PROVEN' for label in outputs):return 'EQUIVALENT',[]
    return 'UNRESOLVED',[]

def qualify_mutant(dataset,unit,mutant,settings,lock,directory,*,calibration=False):
    from .faults import formal_config
    from collect_jasper import make_tcl,parse_log
    directory=Path(directory);result_file=directory/'result.json'
    identity={'version':VERSION,'environment':lock.identity,'unit':unit,'mutant':mutant.get('sha256'),
              'settings':{k:settings.get(k) for k in ('proof_timeout_seconds','jasper_executable','jasper_version_tag')},'calibration':calibration}
    identity['reset_encoding']='native_reset_pin_list_v1'
    if calibration:
        import hashlib
        from . import calibration_cut
        identity['calibration_method']='flat_transition_first_v1'
        identity['calibration_transform_sha256']=hashlib.sha256(Path(calibration_cut.__file__).read_bytes()).hexdigest()
    key=digest(identity)
    if result_file.is_file():
        old=read(result_file)
        if old.get('key')==key and old.get('status') in {'DIFFERENT','EQUIVALENT','INTERFACE_INCOMPATIBLE'}:
            if all(Path(p).is_file() for p in old.get('evidence_files',[])):
                print(f"[POOL_CACHE] {unit['name']} {mutant['id']} {old['status']}",flush=True);return old
    directory.mkdir(parents=True,exist_ok=True);lock.check()
    job=None
    try:
        files=[resolve(dataset,f) for f in unit['rtl_files']+unit.get('environment_files',[])]
        includes=[resolve(dataset,f) for f in unit.get('include_dirs',[])]
        ref=expanded(files,includes,unit.get('defines',[]))
        candidate=ref if calibration else expanded([Path(mutant['file']) if str(f)==mutant['replace'] else f for f in files],includes,unit.get('defines',[]))
        job=JasperAttempt(directory)
        fixed=job.directory/'fixed_context.sv';fixed.write_text(render(dataset,unit,[]))
        job.publish('fixed_context.sv')
        harness=expanded([fixed]+[resolve(dataset,f) for f in unit.get('bind_files',[])],includes,unit.get('defines',[]))
        if calibration:
            from .calibration_cut import flat_transition,run_calibration,UnsupportedCut
            try:
                cut,certificate=flat_transition(ref,unit)
            except UnsupportedCut as unsupported:
                print(f"[POOL_EQ_CALIBRATION] {unit['name']} sequential fallback: {unsupported}",flush=True)
            else:
                result=run_calibration(dataset,unit,ref,harness,cut,certificate,settings,lock,job)
                result.update(key=key,mutant_id=mutant['id'],protocol=VERSION,calibration=True)
                save(result_file,result);lock.check();return result
        source,info=miter_source(ref,candidate,harness,unit,calibration=calibration)
        if source is None:result=info
        else:
            sv=job.directory/'miter.sv';sv.write_text(source)
            job.publish('miter.sv')
            labels=[*info['outputs'],*info.get('calibration_lemmas',{}),'EQ_ENV_WITNESS']
            if calibration:
                print(f"[POOL_EQ_CALIBRATION] {unit['name']} method={info['calibration_method']} "
                      f"state_registers={len(info['state_relation_registers'])} obligations={len(labels)}; "
                      "all base/step/output obligations must be PROVEN and environment witness must be CEX",flush=True)
            cfg=formal_config(dataset,unit,sv,labels,settings)
            cfg.update(top='EQ_MITER',target_scope='EQ_MITER',rtl_files=[],bind_files=[])
            cfg['reset_expression']=info.get('formal_reset_expression',unit.get('reset_expression',''))
            script=make_tcl(cfg,[],sv,[],[],[],expected=labels)
            tcl=job.directory/'run.tcl';tcl.write_text(script);log=job.log
            job.publish('run.tcl')
            print(f"[POOL_EQ] {unit['name']} {mutant['id']} -> {log}",flush=True)
            announced=set()
            def progress(line):
                # The actual Jasper message already identifies the selected
                # property. Show its first start, not every engine/trace retry.
                match=re.search(r'Starting proof for property "(?:[^" ]*\.)?(EQ_(?:CAL_\w+|OUTPUT_\d+|ENV_WITNESS))"',line)
                if match and match[1] not in announced:
                    announced.add(match[1]);print(f"[POOL_EQ_PROGRESS] {unit['name']} {match[1]} started",flush=True)
                elif line.startswith('FH_PROPERTY\t'):
                    print(f"[POOL_EQ_PROGRESS] {unit['name']} {line.strip()}",flush=True)
            code=job.run(settings['jasper_executable'],tcl,settings['proof_timeout_seconds'],on_line=progress)
            if code:raise UnsupportedEquivalence(f'Jasper exited {code}: {error_details(log)}; inspect {log}')
            statuses=parse_log(log.read_text(errors='replace'),labels)
            status,affected=classify_calibration(statuses,info) if calibration else classify(statuses,info['outputs'])
            reason='observable output comparison under the fixed reference environment'
            if calibration:
                failed={label:statuses.get(label,'MISSING') for label in labels
                        if statuses.get(label)!=('CEX' if label=='EQ_ENV_WITNESS' else 'PROVEN')}
                reason='calibration '+info['calibration_method']+(': incomplete/failed obligations '+str(failed) if failed else ': all proof obligations passed')
                print(f"[POOL_EQ_CALIBRATION] {unit['name']} method={info['calibration_method']} status={status}; {reason}",flush=True)
            result={**info,'status':status,'proof_statuses':statuses,'affected_outputs':affected,
                    'witness':{'status':'CEX' if affected else 'NONE','affected_outputs':affected,'trace_status':'NOT_EXPORTED','proof_log':str(log)},
                    'evidence_files':[str(sv),str(tcl),str(log)],'reason':reason}
    except (UnsupportedEquivalence,ValueError,TimeoutError) as e:
        result={'status':'UNRESOLVED','reason':str(e),'evidence_files':
                [str(p) for p in (job.directory/'miter.sv',job.directory/'run.tcl',job.log) if p.is_file()] if job else []}
    result.update(key=key,mutant_id=mutant['id'],protocol=VERSION,calibration=calibration)
    save(result_file,result);lock.check();return result
