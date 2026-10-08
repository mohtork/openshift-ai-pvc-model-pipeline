"""Admin-only prepare/start/watch/evidence/cleanup for the Ceph Podman pipeline."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import yaml

ROOT = Path(__file__).resolve().parents[1]
NS = os.environ.get('NS', 'rag-tekton-lab')
OUT = Path(os.environ.get('WORK_DIR', str(ROOT / 'customer-test' / NS))).resolve()


def oc(*args, data=None, quiet=False):
    command = ['oc', *args]
    result = subprocess.run(command, input=data, text=True, capture_output=True)
    if result.returncode:
        # Secret operations suppress payload/error output; other diagnostics are useful.
        raise RuntimeError('oc command failed: ' + ' '.join(args) + ('' if quiet else '\n' + result.stderr.strip()))
    return result.stdout


def get(kind, name=None, namespace=NS):
    args = ['get', kind]
    if name:
        args.append(name)
    if namespace:
        args += ['-n', namespace]
    return json.loads(oc(*args, '-o', 'json'))


def apply(obj, secret=False):
    obj.setdefault('metadata', {})['namespace'] = NS
    output = oc('apply', '-f', '-', data=json.dumps(obj), quiet=secret)
    print(output.strip(), flush=True)


def name(value):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise RuntimeError('Invalid namespace/name/directory: ' + value)
    return value


def configuration():
    run = yaml.safe_load((ROOT / 'tekton/pipelinerun-lab.yaml').read_text())
    defaults = {p['name']:str(p['value']) for p in run['spec']['params']}
    values = {k:os.environ.get(k, v) for k,v in defaults.items()}
    # Require explicit SC selection; example names are never guessed at install time.
    for key in ('RBD_STORAGE_CLASS', 'CEPHFS_STORAGE_CLASS'):
        if not os.environ.get(key):
            raise RuntimeError('Set ' + key + ' after oc get sc')
    name(NS)
    for key in ('MODEL_NAME', 'MODEL_DIR', 'MODEL_PVC', 'SERVING_RUNTIME'):
        name(values[key])
    if not re.fullmatch(r'[1-9][0-9]*', values['GPU_COUNT']):
        raise RuntimeError('GPU_COUNT must be a positive integer')
    for key in ('MIN_STAGING_GIB','MIN_MODEL_GIB','READY_TIMEOUT_SECONDS'):
        if not re.fullmatch(r'[1-9][0-9]*',values[key]):
            raise RuntimeError(key + ' must be a positive integer')
    # Outer Task images are platform-pinned in Task manifests, not Pipeline parameters.
    return run,values


def render(fs_group):
    run, values = configuration()
    if not re.fullmatch(r'[1-9][0-9]*', str(fs_group)):
        raise RuntimeError('FSGROUP must be discovered from namespace, never zero/guessed')
    OUT.mkdir(parents=True, exist_ok=True)
    manifests = OUT / 'manifests'
    manifests.mkdir(exist_ok=True)
    for source in sorted((ROOT / 'tekton').glob('*.yaml')):
        if source.name == 'pipelinerun-lab.yaml':
            continue
        obj = yaml.safe_load(source.read_text())
        obj['metadata']['namespace'] = NS
        if obj['kind'] == 'ConfigMap':
            # Pin the chosen admin values; researchers cannot edit this catalog.
            original = json.loads(obj['data']['approved.json'])
            keys = next(iter(original.values())).keys()
            obj['data']['approved.json'] = json.dumps({values['MODEL_NAME']:{k:values[k] for k in keys}},indent=2)
        (manifests/source.name).write_text(yaml.safe_dump(obj,sort_keys=False))
    for source in sorted((ROOT / 'tekton/rbac').glob('*.yaml')):
        obj = yaml.safe_load(source.read_text())
        obj['metadata']['namespace'] = NS
        for subject in obj.get('subjects',[]):
            if subject['kind']=='ServiceAccount':
                subject['namespace']=NS
        (manifests/source.name).write_text(yaml.safe_dump(obj,sort_keys=False))
    pvc = yaml.safe_load((ROOT / 'storage/model-pvc.yaml').read_text())
    pvc['metadata'].update(name=values['MODEL_PVC'],namespace=NS)
    pvc['spec']['storageClassName']=values['CEPHFS_STORAGE_CLASS']
    pvc['spec']['resources']['requests']['storage']=os.environ.get('MODEL_SIZE','20Gi')
    (manifests/'model-pvc.yaml').write_text(yaml.safe_dump(pvc,sort_keys=False))
    run['metadata']={'generateName':'ceph-model-','namespace':NS,'labels':{'app.kubernetes.io/part-of':'ceph-model-evaluation'}}
    run['spec']['params']=[{'name':k,'value':v} for k,v in values.items()]
    template=run['spec']['workspaces'][0]['volumeClaimTemplate']['spec']
    template['storageClassName']=values['RBD_STORAGE_CLASS']
    template['resources']['requests']['storage']=values['STAGING_SIZE']
    run['spec']['taskRunSpecs'][0]['podTemplate']['securityContext']['fsGroup']=int(fs_group)
    # Optional placement/tolerations affect only extraction, never force GPU staging.
    if os.environ.get('STAGING_NODE'):
        run['spec']['taskRunSpecs'][0]['podTemplate']['nodeSelector']={'kubernetes.io/hostname':os.environ['STAGING_NODE']}
    if os.environ.get('STAGING_TOLERATIONS_JSON'):
        tolerations=json.loads(os.environ['STAGING_TOLERATIONS_JSON'])
        if not isinstance(tolerations,list): raise RuntimeError('Tolerations must be a JSON list')
        run['spec']['taskRunSpecs'][0]['podTemplate']['tolerations']=tolerations
    (OUT/'pipelinerun.yaml').write_text(yaml.safe_dump(run,sort_keys=False))
    print('Rendered customer resources to '+str(OUT))
    return values


def prepare():
    _, values = configuration()
    # Only read-only preflight before project/resources are created.
    print('Cluster: '+oc('whoami','--show-server').strip())
    # Current release is administrator-operated. These checks intentionally require admin/platform capabilities.
    if oc('auth','can-i','get','secret/pull-secret','-n','openshift-config').strip() != 'yes':
        raise RuntimeError('ADMIN-ONLY: current user cannot read openshift-config/pull-secret')
    if oc('auth','can-i','use','scc/privileged').strip() != 'yes':
        raise RuntimeError('ADMIN-ONLY: current user cannot use privileged SCC')
    for crd in ('tasks.tekton.dev','pipelines.tekton.dev','pipelineruns.tekton.dev',
                'inferenceservices.serving.kserve.io','servingruntimes.serving.kserve.io'):
        get('crd',crd,namespace=None)
    for key, driver in [('RBD_STORAGE_CLASS','rbd.csi.ceph.com'),('CEPHFS_STORAGE_CLASS','cephfs.csi.ceph.com')]:
        sc=get('sc',values[key],namespace=None)
        if driver not in sc.get('provisioner',''):
            raise RuntimeError(f'{key} does not identify expected Ceph CSI provisioner; inspect class')
    runtime_file=os.environ.get('RUNTIME_MANIFEST')
    if not runtime_file:
        raise RuntimeError('Set RUNTIME_MANIFEST to a reviewed, customer-compatible vLLM ServingRuntime YAML')
    runtime=yaml.safe_load(Path(runtime_file).read_text())
    if runtime.get('kind')!='ServingRuntime' or runtime['metadata']['name']!=values['SERVING_RUNTIME']:
        raise RuntimeError('Runtime manifest must be one ServingRuntime with the configured name')
    if any('modelcar' in c.get('image','').lower() for c in runtime['spec']['containers']):
        raise RuntimeError('ServingRuntime cannot use ModelCar')
    existing=oc('get','namespace',NS,'--ignore-not-found','-o','name').strip()
    if existing and os.environ.get('USE_EXISTING_NAMESPACE')!='yes':
        raise RuntimeError('Namespace exists. Use a new isolated namespace, or review it and explicitly set USE_EXISTING_NAMESPACE=yes')
    if existing:
        # Never mutate an existing PVC or privilege an existing extractor identity.
        for kind, resource in [('pvc',values['MODEL_PVC']),('sa','podman-model-extractor')]:
            if oc('get',kind,resource,'-n',NS,'--ignore-not-found','-o','name').strip():
                raise RuntimeError('Existing PVC/extractor identity; refusing to overwrite. Use a fresh namespace.')
    print(f'Will create test resources in {NS}; permanent CephFS PVC, two service accounts, catalog, four Tasks and Pipeline.')
    print('Will copy the lab pull secret and grant privileged SCC only to podman-model-extractor. No operators installed.')
    if not existing:
        oc('new-project',NS)
    annotation=get('namespace',NS,namespace=None)['metadata'].get('annotations',{}).get('openshift.io/sa.scc.supplemental-groups','')
    fs_group=annotation.split('/')[0]
    values=render(fs_group)
    manifests=OUT/'manifests'
    # Registry contents never reach stdout, logs, generated files, or env vars.
    raw=oc('get','secret','pull-secret','-n','openshift-config','-o','json',quiet=True)
    source=json.loads(raw)['data']['.dockerconfigjson']
    decoded=base64.b64decode(source)
    if not json.loads(decoded).get('auths'):
        raise RuntimeError('Global pull secret has no registry auth entries')
    # Reproduce the lab's restrictive temporary-file provisioning; always unlink.
    descriptor,path=tempfile.mkstemp(prefix='ocp-pull-secret-',suffix='.json')
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'wb') as handle: handle.write(decoded)
        secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'redhat-registry-auth'},
                'type':'kubernetes.io/dockerconfigjson','data':{'.dockerconfigjson':source}}
        apply(secret,secret=True)
    finally:
        Path(path).unlink(missing_ok=True)
    # Validate all generated resource shapes through the server before installing.
    for file in sorted(manifests.glob('*.yaml')):
        if file.name.endswith('.example.yaml'):
            continue
        oc('apply','--dry-run=server','-f',str(file))
    for file in sorted(manifests.glob('*serviceaccount.yaml')):
        oc('apply','-f',str(file))
    oc('secrets','link','podman-model-extractor','redhat-registry-auth','--for=pull','-n',NS)
    # Default account is serving only; no privileged SCC grant.
    oc('adm','policy','add-scc-to-user','privileged','-z','podman-model-extractor','-n',NS)
    for file in sorted(manifests.glob('*.yaml')):
        if file.name.endswith('.example.yaml'):
            continue
        print(oc('apply','-f',str(file)).strip())
    runtime['metadata'].pop('namespace',None)
    apply(runtime)
    # Immediate-binding CephFS is expected; fail before creating a run if unbound.
    oc('wait','pvc/'+values['MODEL_PVC'],'-n',NS,'--for=jsonpath={.status.phase}=Bound','--timeout=5m')
    oc('create','--dry-run=server','-f',str(OUT/'pipelinerun.yaml'))
    print('Preparation complete. Start: bash scripts/admin-lab.sh start')


def run_name():
    value=os.environ.get('RUN')
    if not value:
        value=(OUT/'run-name.txt').read_text().strip()
    return name(value)


def result_map(obj):
    return {item['name']:item.get('value') for item in obj.get('status',{}).get('results',[])}


def watch():
    run=run_name()
    deadline=time.monotonic()+int(os.environ.get('WATCH_TIMEOUT_SECONDS','5500'))
    while time.monotonic()<deadline:
        obj=get('pipelinerun',run)
        condition=next((c for c in obj.get('status',{}).get('conditions',[]) if c['type']=='Succeeded'),{})
        print(f"{run}: {condition.get('status','Pending')} {condition.get('reason','')}",flush=True)
        if condition.get('status') in ('True','False'):
            print(json.dumps(result_map(obj),indent=2))
            if condition['status']=='False': raise RuntimeError(condition.get('message','Pipeline failed'))
            return
        time.sleep(15)
    raise RuntimeError('Monitor timeout; run has not been cancelled')


def collect():
    run=run_name()
    out=OUT/'evidence'/run
    out.mkdir(parents=True,exist_ok=True)
    for kind in ('pipelinerun','taskruns','pods','pvc','inferenceservices','events'):
        args=['get',kind,'-n',NS,'-o','yaml']
        if kind=='pipelinerun': args.insert(2,run)
        (out/(kind+'.yaml')).write_text(oc(*args))
    for pod in get('pods')['items']:
        if pod['metadata'].get('labels',{}).get('tekton.dev/pipelineRun')==run or 'serving.kserve.io/inferenceservice' in pod['metadata'].get('labels',{}):
            for container in pod['spec']['containers']:
                result=subprocess.run(['oc','logs',pod['metadata']['name'],'-n',NS,'-c',container['name']],capture_output=True,text=True)
                (out/(pod['metadata']['name']+'-'+container['name']+'.log')).write_text(result.stdout+result.stderr)
    print('Evidence: '+str(out)+' (Secrets are excluded)')


def cleanup_staging():
    run=run_name()
    obj=get('pipelinerun',run)
    if not obj.get('status',{}).get('completionTime'):
        raise RuntimeError('Run still active; refusing PVC deletion')
    tasks=get('taskruns')['items']
    extraction=[t for t in tasks if t['metadata'].get('labels',{}).get('tekton.dev/pipelineRun')==run and t['metadata'].get('labels',{}).get('tekton.dev/pipelineTask')=='extract-model']
    if len(extraction)!=1 or result_map(extraction[0]).get('MODEL_VALIDATED')!='true':
        raise RuntimeError('No validated extraction result; retain staging for diagnosis')
    task=extraction[0]
    if not task.get('status',{}).get('completionTime'):
        raise RuntimeError('Extraction Task still active')
    pod_name=task.get('status',{}).get('podName')
    if not pod_name: raise RuntimeError('No extraction pod record')
    pod=get('pod',pod_name)
    if pod['status'].get('phase') not in ('Succeeded','Failed'):
        raise RuntimeError('Extraction pod not terminated')
    staging=[w for w in task['spec'].get('workspaces',[]) if w['name']=='podman-storage']
    claim=staging[0]['persistentVolumeClaim']['claimName']
    pvc=get('pvc',claim)
    labels=pvc['metadata'].get('labels',{})
    if labels.get('model-storage-role')!='temporary-podman':
        raise RuntimeError('PVC is not marked as temporary staging')
    owners=pvc['metadata'].get('ownerReferences',[])
    if not any(o['uid']==obj['metadata']['uid'] and o['kind']=='PipelineRun' for o in owners):
        raise RuntimeError('Staging PVC owner does not match this run')
    print('Deleting validated temporary staging PVC only: '+claim)
    print(oc('delete','pvc',claim,'-n',NS).strip())


def verify_nodes():
    run=run_name()
    obj=get('pipelinerun',run)
    if not any(c['type']=='Succeeded' and c['status']=='True' for c in obj.get('status',{}).get('conditions',[])):
        raise RuntimeError('Inference pipeline must succeed before node acceptance')
    results=result_map(obj)
    gpu_node=results.get('GPU_NODE')
    predictor=results.get('PREDICTOR_POD')
    if not gpu_node or not predictor:
        raise RuntimeError('Missing predictor placement results')
    pod=get('pod',predictor)
    runtime_images=[c['image'] for c in pod['spec']['containers'] if c['name']=='kserve-container']
    if not runtime_images:
        raise RuntimeError('Inspect adapted runtime container name; expected kserve-container')
    nodes={gpu_node}
    for candidate in get('pods')['items']:
        labels=candidate['metadata'].get('labels',{})
        if labels.get('tekton.dev/pipelineRun')==run and labels.get('tekton.dev/pipelineTask')=='extract-model':
            nodes.add(candidate['spec']['nodeName'])
    out=OUT/'evidence'/run
    out.mkdir(parents=True,exist_ok=True)
    for node in sorted(nodes):
        raw=oc('debug','node/'+node,'--quiet','--','chroot','/host','crictl','images','-o','json')
        # Debug wrappers may add a banner. Parse an actual images JSON document.
        decoder=json.JSONDecoder()
        inventory=None
        for index,char in enumerate(raw):
            if char=='{':
                try:
                    value,_=decoder.raw_decode(raw[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(value,dict) and isinstance(value.get('images'),list):
                    inventory=value
                    break
        if inventory is None:
            raise RuntimeError('Unable to parse CRI-O image inventory; acceptance incomplete')
        (out/(node+'-crio-images.json')).write_text(json.dumps(inventory,indent=2))
        references=[ref for image in inventory['images'] for field in ('repoTags','repoDigests') for ref in (image.get(field) or [])]
        if any('modelcar' in ref.lower() for ref in references):
            raise RuntimeError('FAIL: ModelCar present in node CRI-O inventory on '+node+'; inspect baseline, never remove automatically')
        if node==gpu_node:
            for image in runtime_images:
                repository=image.split('@')[0].rsplit(':',1)[0] if ':' in image.split('/')[-1] else image.split('@')[0]
                if not any(ref.startswith(repository+':') or ref.startswith(repository+'@') for ref in references):
                    raise RuntimeError('Serving runtime image absent from GPU CRI-O inventory')
        print('PASS CRI-O inventory: no ModelCar on '+node)
    (out/'node-acceptance.json').write_text(json.dumps({'run':run,'gpu_node':gpu_node,'nodes_checked':sorted(nodes),'modelcar_absent':True},indent=2))


def main():
    action=sys.argv[1] if len(sys.argv)>1 else 'help'
    if action=='prepare': prepare()
    elif action=='render': render(os.environ.get('FSGROUP',''))
    elif action=='start':
        doc=yaml.safe_load((OUT/'pipelinerun.yaml').read_text())
        fs=doc['spec']['taskRunSpecs'][0]['podTemplate']['securityContext']['fsGroup']
        if not isinstance(fs,int) or fs<=0: raise RuntimeError('Unrendered fsGroup')
        run=oc('create','-f',str(OUT/'pipelinerun.yaml'),'-o','jsonpath={.metadata.name}').strip()
        (OUT/'run-name.txt').write_text(run+'\n')
        print('RUN='+run)
    elif action=='watch': watch()
    elif action=='evidence': collect()
    elif action=='cleanup-staging': cleanup_staging()
    elif action=='verify-node': verify_nodes()
    else:
        print('Usage: bash scripts/admin-lab.sh prepare|render|start|watch|evidence|verify-node|cleanup-staging')
        return


if __name__=='__main__':
    try:
        main()
    except (RuntimeError,KeyError,ValueError,OSError) as error:
        raise SystemExit(str(error))
