"""Conservative combinational induction step for flat single-clock RTL state.

Only the golden self-calibration uses this transform. Actual mutant miters and
dataset files are never transformed. Unsupported syntax falls back to the
existing fully proved sequential calibration; no speculative equivalence pass.
"""
from __future__ import annotations
import re

METHOD = 'flat_next_state_combinational_v1'


class UnsupportedCut(ValueError):
    pass


def flat_transition(source, unit):
    from .equivalence import slang, reachable_source, design_info, UnsupportedEquivalence
    source = reachable_source(source, unit['top'])
    if re.search(r'\$(?!clog2\b|bits\b|signed\b|unsigned\b)[A-Za-z_]\w*',source):
        raise UnsupportedCut('runtime system functions require sequential calibration')
    info = design_info(source, unit['top'])
    states = info['states']
    if unit['clock_mode'] != 'clocked' or not states:
        raise UnsupportedCut('requires clocked state')
    if info['initialized_states']:
        raise UnsupportedCut('explicit initial state requires sequential base proof')
    if any(not re.fullmatch(r'[A-Za-z_]\w*', s) for s in states):
        raise UnsupportedCut('hierarchical state requires sequential calibration')
    p = slang(); tree = p.syntax.SyntaxTree.fromText(source)
    modules = []; edits = []; owners = {}; blocks = []
    def span(n): return n.sourceRange.start.offset, n.sourceRange.end.offset
    def kind(n): return str(n.kind).split('.')[-1]
    def walk(n):
        k = kind(n)
        if k == 'ModuleDeclaration' and n.header.name.rawText == unit['top']:
            modules.append(n)
        if k in {'InitialBlock', 'FinalBlock', 'FunctionDeclaration', 'TaskDeclaration', 'AlwaysLatchBlock'}:
            raise UnsupportedCut('unsupported storage/behavior: ' + k)
        if 'increment' in k.lower() or 'decrement' in k.lower():
            raise UnsupportedCut('increment/decrement side effects require sequential calibration')
        if 'Assume' in k or 'Restrict' in k:
            raise UnsupportedCut('internal RTL constraints require sequential calibration')
        if 'Assertion' in k or 'AssertProperty' in k or 'CoverProperty' in k:
            raise UnsupportedCut('RTL assertion actions require sequential calibration')
        # Do not introduce a different initial-value domain through hidden
        # statically initialized variables. Parameters/genvars are not storage.
        if k == 'Declarator' and getattr(n,'initializer',None) is not None:
            parent=kind(n.parent)
            if parent not in {'ParameterDeclaration','TypeParameterDeclaration','GenvarDeclaration'}:
                raise UnsupportedCut('initialized declaration requires sequential calibration')
        if k in {'AlwaysBlock','AlwaysFFBlock','AlwaysCombBlock'}:
            blocks.append(n)
    tree.root.visit(walk)
    if len(modules) != 1:
        raise UnsupportedCut('cannot identify one top declaration')
    top = modules[0]
    # Unsized decimal case labels can widen the semantic case expression. For
    # exhaustive cases over a simple unsigned signal, use the original signal
    # domain, not that comparison-conversion width. Parameterized instances must
    # all be covered; unsupported expressions retain the sequential fallback.
    case_domains={}
    options=p.ast.CompilationOptions();options.topModules={unit['top']}
    bag=p.Bag();bag.compilationOptions=options
    compilation=p.ast.Compilation(bag);compilation.addSyntaxTree(tree)
    def case_domain(node):
        if str(node.kind).split('.')[-1]!='Case':return
        syntax=node.syntax
        if not re.fullmatch(r'[A-Za-z_]\w*',str(syntax.expr).strip()):return
        expr=node.expr
        while str(expr.kind).split('.')[-1]=='Conversion':expr=expr.operand
        symbol=expr.getSymbolReference()
        if symbol is not None and not symbol.type.isSigned and symbol.type.bitWidth:
            case_domains.setdefault(syntax.sourceRange.start.offset,set()).add(symbol.type.bitWidth)
    compilation.getRoot().visit(case_domain)
    current={s:'EQ_CUR_'+s for s in states};following={s:'EQ_NEXT_'+s for s in states}
    identifiers=set(re.findall(r'\b[A-Za-z_]\w*\b',source))
    if identifiers & set(current.values()) or identifiers & set(following.values()):
        raise UnsupportedCut('generated state port name collision')
    for block in blocks:
        statement=block.statement
        control=getattr(statement,'timingControl',None)
        text=str(control) if control is not None else ''
        edge=bool(re.search(r'\b(?:posedge|negedge|edge)\b',text))
        if not edge:
            # Prove definite assignment on every branch, and reject reads of
            # a written object before its value is established. This excludes
            # hidden latches/feedback while accepting complete if/else logic.
            body=statement.statement if control is not None else statement
            assigned=set();defaults=set()
            def writes(n):
                if kind(n) in {'AssignmentExpression','BlockingAssignmentExpression','NonblockingAssignmentExpression'}:
                    a,b=span(n.left)
                    m=re.match(r'^\s*([A-Za-z_]\w*)',source[a:b])
                    if m:assigned.add(m[1])
            body.visit(writes)
            if assigned & set(states) and span(top)[0] <= span(block)[0] < span(top)[1]:
                raise UnsupportedCut('state also has a combinational procedural driver')
            def reads(node,known):
                a,b=span(node)
                names=set(re.findall(r'\b[A-Za-z_]\w*\b',source[a:b]))
                if (names & assigned)-known:
                    raise UnsupportedCut('combinational read before definite assignment')
            def definite(node,known):
                known=set(known);k=kind(node)
                if k=='SequentialBlockStatement':
                    for item in node.items:known=definite(item,known)
                    return known
                if k=='ConditionalStatement':
                    reads(node.predicate,known)
                    yes=definite(node.statement,known)
                    no=definite(node.elseClause.clause,known) if node.elseClause is not None else known
                    return yes & no
                if k=='CaseStatement':
                    reads(node.expr,known);branches=[];values=set();has_default=False
                    for item in node.items:
                        ik=kind(item)
                        if ik=='DefaultCaseItem':has_default=True
                        elif ik=='StandardCaseItem':
                            for expr in item.expressions:
                                if not hasattr(expr,'sourceRange'):continue
                                a,b=span(expr);value=source[a:b].strip()
                                if not re.fullmatch(r'\d+',value):
                                    raise UnsupportedCut('nondecimal case domain requires sequential calibration')
                                values.add(int(value))
                        else:raise UnsupportedCut('unsupported case item')
                        branches.append(definite(item.clause,known))
                    domains=case_domains.get(node.sourceRange.start.offset,set())
                    complete=has_default or bool(domains) and all(len(values)==(1<<width) and min(values)==0 and max(values)==(1<<width)-1 for width in domains)
                    if not complete:branches.append(known)
                    return set.intersection(*branches) if branches else known
                if k=='EmptyStatement':return known
                if k=='ExpressionStatement' and kind(node.expr) in {'AssignmentExpression','BlockingAssignmentExpression'}:
                    expr=node.expr;reads(expr.right,known)
                    a,b=span(expr.left);lhs=source[a:b].strip()
                    if re.fullmatch(r'[A-Za-z_]\w*',lhs):known.add(lhs)
                    else:
                        match=re.match(r'([A-Za-z_]\w*)',lhs)
                        if not match or match[1] not in known:
                            raise UnsupportedCut('partial combinational write before full assignment')
                        indices=set(re.findall(r'\b[A-Za-z_]\w*\b',lhs[len(match[1]):]))
                        if (indices & assigned)-known:
                            raise UnsupportedCut('combinational index read before definite assignment')
                    return known
                raise UnsupportedCut('unsupported combinational statement: '+k)
            defaults=definite(body,set())
            if assigned-defaults:
                raise UnsupportedCut('combinational block without unconditional defaults: '+','.join(sorted(assigned-defaults)))
            continue
        if not re.fullmatch(r'\s*@\s*\(\s*posedge\s+'+re.escape(unit['clock'])+r'\s*\)\s*',text):
            raise UnsupportedCut('requires exactly the configured rising-edge clock, no asynchronous events')
        # Only top-level registers are supported. Child storage was rejected
        # above from elaborated paths, not guessed from textual module names.
        if not (span(top)[0] <= span(block)[0] < span(top)[1]):
            raise UnsupportedCut('child sequential process')
        body=statement.statement;start,end=span(body);local=[];written=set()
        def convert(n):
            k=kind(n)
            if k=='NonblockingAssignmentExpression':
                a,b=span(n.left);lhs=source[a:b].strip()
                if lhs not in states:
                    raise UnsupportedCut('only full-register nonblocking assignments are cut')
                written.add(lhs)
                a,b=span(n.left);local.append((a-start,b-start,following[lhs]))
                token=n.operatorToken;local.append((token.location.offset-start,token.location.offset-start+len(token.rawText),'='))
            elif k in {'AssignmentExpression','BlockingAssignmentExpression','TimingControlStatement','WaitStatement','ProceduralAssignStatement','ForceStatement','ReleaseStatement'} or 'DelayControl' in k:
                raise UnsupportedCut('unsupported sequential statement: '+k)
        body.visit(convert)
        if not written:
            raise UnsupportedCut('sequential process has no supported state assignment')
        for s in written:
            if s in owners:raise UnsupportedCut('multiple procedural drivers: '+s)
            owners[s]=True
        body_text=source[start:end]
        for a,b,new in sorted(local,reverse=True):body_text=body_text[:a]+new+body_text[b:]
        defaults='\n'.join(f'{following[s]} = {s};' for s in sorted(written))
        a,b=span(block);edits.append((a,b,'always_comb begin\n'+defaults+'\n'+body_text+'\nend\n'))
    if set(owners)!=set(states):
        raise UnsupportedCut('not every elaborated register has one supported writer')
    ports=top.header.ports
    if ports is None:raise UnsupportedCut('missing port list')
    ansi=kind(ports)=='AnsiPortList'
    declarations=[];names=[]
    for s in sorted(states):
        for direction,name in (('input',current[s]),('output',following[s])):
            declaration=direction+' '+states[s]['type']+' '+name
            names.append(declaration if ansi else name)
            if not ansi:declarations.append(declaration+';')
    position=ports.closeParen.location.offset
    edits.append((position,position,', '+', '.join(names)))
    declaration_position=top.header.semi.location.offset+len(top.header.semi.rawText)
    edits.append((declaration_position,declaration_position,'\n'+'\n'.join(declarations)+'\n'))
    assignments='\n'.join(f'assign {s} = {current[s]};' for s in sorted(states))+'\n'
    end_position=top.getLastToken().location.offset
    edits.append((end_position,end_position,assignments))
    for a,b,new in sorted(edits,reverse=True):source=source[:a]+new+source[b:]
    try:
        cut_info=design_info(source,unit['top'])
    except UnsupportedEquivalence as error:
        raise UnsupportedCut('cut elaboration is unsupported: '+str(error)) from error
    if cut_info['states']:
        raise UnsupportedCut('cut still contains sequential storage')
    return source,{'method':METHOD,'registers':sorted(states),
                   'state_bits':sum(s['width'] for s in states.values()),
                   'current_ports':current,'next_ports':following,
                   'base':'original matching unconstrained initial-register correspondence; no explicit initializers',
                   'step':'unconditional combinational next-state equality for all common state/input values',
                   'environment':'checked separately under the original fixed assumptions'}


def run_calibration(dataset,unit,reference,harness,cut,certificate,settings,lock,first_job):
    """Prove the cut transition/output functions and the actual environment.

    The induction base is the original initial-register coupling, not a new
    assumption. Every possible common state value is a free input in the
    combinational step. The fixed environment is NOT applied to this stronger
    step proof; it is checked unchanged in a separate clocked reachability job.
    """
    import time
    from .common import save
    from .equivalence import miter_source,UnsupportedEquivalence
    from .faults import formal_config
    from .jasper_jobs import JasperAttempt,error_details
    from collect_jasper import make_tcl,parse_log
    started=time.monotonic();root=first_job.root;evidence=[];phases={}
    cut_unit={**unit,'clock_mode':'none','clock':'','reset_expression':''}
    comb,comb_info=miter_source(cut,cut,'',cut_unit)
    environment,environment_info=miter_source(reference,reference,harness,unit)
    if set(environment_info['coupled_initial_registers'])!=set(certificate['registers']):
        raise UnsupportedEquivalence('Flat calibration base does not match original initial-state coupling')
    # Remove only the generated output assertions in the generated wrapper.
    # Original RTL, fixed assumptions and initial coupling remain byte-identical.
    marker='module EQ_MITER('
    position=environment.rfind(marker)
    if position<0:raise UnsupportedEquivalence('Missing generated environment wrapper')
    prefix=environment[:position];wrapper=environment[position:]
    labels=set(environment_info['outputs'])
    wrapper='\n'.join(line for line in wrapper.splitlines()
                      if not any(line.startswith(label+': ') for label in labels))+'\n'
    environment=prefix+wrapper
    save(root/'transition_certificate.json',certificate)
    evidence.append(str(root/'transition_certificate.json'))
    jobs=[('next_state',first_job,comb,[*comb_info['outputs'],'EQ_ENV_WITNESS'],cut_unit),
          ('environment',None,environment,['EQ_ENV_WITNESS'],unit)]
    for phase,job,source,expected,phase_unit in jobs:
        lock.check()
        job=job or JasperAttempt(root)
        sv=job.directory/'miter.sv';sv.write_text(source);job.publish('miter.sv')
        config=formal_config(dataset,phase_unit,sv,expected,settings)
        config.update(top='EQ_MITER',target_scope='EQ_MITER',rtl_files=[],bind_files=[])
        if phase=='environment':
            config['reset_expression']=environment_info.get('formal_reset_expression',unit.get('reset_expression',''))
        script=job.directory/'run.tcl'
        script.write_text(make_tcl(config,[],sv,[],[],[],expected=expected));job.publish('run.tcl')
        evidence.extend([str(sv),str(script),str(job.log)])
        print(f"[POOL_EQ_CALIBRATION] {unit['name']} method={METHOD} phase={phase} "
              f"clock_mode={phase_unit['clock_mode']} state_bits={certificate['state_bits']} obligations={len(expected)}",flush=True)
        print(f"[POOL_EQ] {unit['name']} golden_calibration/{phase} -> {job.log}",flush=True)
        announced=set()
        def progress(line):
            match=re.search(r'Starting proof for property "([^" ]+)"',line)
            if match and match[1] not in announced:
                announced.add(match[1]);print(f"[POOL_EQ_PROGRESS] {unit['name']} {phase} {match[1]} started",flush=True)
            elif line.startswith('FH_PROPERTY\t'):
                print(f"[POOL_EQ_PROGRESS] {unit['name']} {phase} {line.strip()}",flush=True)
        limit=settings['proof_timeout_seconds']
        remaining=limit-(time.monotonic()-started) if limit else limit
        try:
            if limit and remaining<=0:raise TimeoutError('calibration shared time budget exhausted')
            code=job.run(settings['jasper_executable'],script,remaining,on_line=progress)
            if code:raise UnsupportedEquivalence(f'Jasper exited {code}: {error_details(job.log)}; inspect {job.log}')
            statuses=parse_log(job.log.read_text(errors='replace'),expected)
            failed={label:statuses.get(label,'MISSING') for label in expected
                    if statuses.get(label)!=('CEX' if label=='EQ_ENV_WITNESS' else 'PROVEN')}
            phases[phase]={'proof_statuses':statuses,'passed':not failed,'log':str(job.log)}
            reason=f'{phase} obligations '+('passed' if not failed else str(failed))
        except (UnsupportedEquivalence,ValueError,TimeoutError) as error:
            failed={'execution':str(error)};reason=str(error)
            phases[phase]={'passed':False,'error':reason,'log':str(job.log)}
        save(root/'calibration_progress.json',{'method':METHOD,'phases':phases})
        print(f"[POOL_EQ_CALIBRATION] {unit['name']} {phase} {'PASS' if not failed else 'UNRESOLVED'}: {reason}",flush=True)
        if failed:
            return {'status':'UNRESOLVED','reason':reason,'calibration_method':METHOD,
                    'phases':phases,'evidence_files':evidence,'transition_certificate':certificate}
    return {'status':'EQUIVALENT','reason':'unconditional combinational next-state/output equality PROVEN; original fixed environment reachable',
            'calibration_method':METHOD,'phases':phases,'evidence_files':evidence,
            'transition_certificate':certificate,'coupled_initial_registers':certificate['registers'],
            'outputs':environment_info['outputs'],'affected_outputs':[]}
