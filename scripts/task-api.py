"""Embedded in Tekton Python steps. Only Kubernetes service-account auth is used."""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

NS = os.environ['NAMESPACE']
SA = Path('/var/run/secrets/kubernetes.io/serviceaccount')
CA = ssl.create_default_context(cafile=str(SA / 'ca.crt'))
BASE = f'/apis/serving.kserve.io/v1beta1/namespaces/{NS}/inferenceservices'


def api(path, body=None):
    request = urllib.request.Request('https://kubernetes.default.svc' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + (SA / 'token').read_text().strip(),
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, context=CA, timeout=30) as response:
        return json.load(response)


def absent(path):
    try:
        api(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    raise RuntimeError('Existing deployment; refusing overwrite')


def valid_name(value):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
        raise RuntimeError('Invalid Kubernetes name/directory')


def approved():
    # Administrator-owned catalog; a submitted image alone cannot override policy.
    catalog = json.loads(Path('/catalog/approved.json').read_text())
    entry = catalog.get(os.environ['MODEL_NAME'])
    if not entry:
        raise RuntimeError('Model not approved')
    for key, expected in entry.items():
        if os.environ.get(key) != str(expected):
            raise RuntimeError(f'Parameter {key} differs from administrator catalog')
    for key in ('MODEL_NAME', 'MODEL_DIR', 'MODEL_PVC', 'SERVING_RUNTIME'):
        valid_name(os.environ[key])


def pvc(name, sc, mode):
    obj = api(f'/api/v1/namespaces/{NS}/persistentvolumeclaims/{name}')
    if obj['status'].get('phase') != 'Bound':
        raise RuntimeError(f'PVC {name} not Bound')
    spec = obj['spec']
    if spec.get('storageClassName') != sc or mode not in spec['accessModes'] or spec.get('volumeMode', 'Filesystem') != 'Filesystem':
        raise RuntimeError(f'PVC {name} class/access/volume mode mismatch')
    print(f'PVC {name}: Bound, {sc}, {mode}', flush=True)


def main():
    action = os.environ['ACTION']
    approved()
    model = os.environ['MODEL_NAME']
    if action in ('validate', 'bound'):
        pvc(os.environ['MODEL_PVC'], os.environ['CEPHFS_STORAGE_CLASS'], 'ReadWriteMany')
        auth = json.loads(Path('/auth/config.json').read_text())
        if not auth.get('auths'):
            raise RuntimeError('Registry auth file has no auths; contents suppressed')
        if action == 'bound':
            # This step runs in the extraction pod after its workspace is mounted.
            pvc(os.environ['STAGING_PVC'], os.environ['RBD_STORAGE_CLASS'], 'ReadWriteOnce')
        else:
            api(f'/apis/serving.kserve.io/v1alpha1/namespaces/{NS}/servingruntimes/' + os.environ['SERVING_RUNTIME'])
            absent(BASE + '/' + model)
        return
    if action == 'deploy':
        absent(BASE + '/' + model)
        resource = os.environ['GPU_RESOURCE_NAME']
        quantity = {'cpu': os.environ['CPU_REQUEST'], 'memory': os.environ['MEMORY_REQUEST'], resource: os.environ['GPU_COUNT']}
        predictor = {'minReplicas': 1, 'maxReplicas': 1, 'model': {
            'modelFormat': {'name': 'vLLM'}, 'runtime': os.environ['SERVING_RUNTIME'],
            'storageUri': f"pvc://{os.environ['MODEL_PVC']}/{os.environ['MODEL_DIR']}/",
            'resources': {'requests': quantity, 'limits': quantity}}}
        # Predictor deliberately does not use the privileged extraction account.
        annotations = {}
        if os.environ.get('DEPLOYMENT_MODE'):
            annotations['serving.kserve.io/deploymentMode'] = os.environ['DEPLOYMENT_MODE']
        api(BASE, {'apiVersion': 'serving.kserve.io/v1beta1', 'kind': 'InferenceService',
            'metadata': {'name': model, 'annotations': annotations,
                         'labels': {'opendatahub.io/dashboard': 'true'}},
            'spec': {'predictor': predictor}})
        print('Created model with permanent CephFS PVC URI', flush=True)
        return
    if action != 'test':
        raise RuntimeError('Unknown task action')
    started = time.monotonic()
    deadline = started + int(os.environ['READY_TIMEOUT_SECONDS'])
    while time.monotonic() < deadline:
        service = api(BASE + '/' + model)
        conditions = service.get('status', {}).get('conditions', [])
        if any(c['type'] == 'Ready' and c['status'] == 'True' for c in conditions):
            break
        print('Waiting for KServe readiness', flush=True)
        time.sleep(10)
    else:
        raise RuntimeError('KServe readiness timeout')
    elapsed = round(time.monotonic() - started)
    pods = api(f'/api/v1/namespaces/{NS}/pods?labelSelector=serving.kserve.io%2Finferenceservice%3D{model}')['items']
    ready_pods = [p for p in pods if p['status'].get('phase') == 'Running' and any(c['type'] == 'Ready' and c['status'] == 'True' for c in p['status'].get('conditions', []))]
    if not ready_pods:
        raise RuntimeError('No ready predictor pod')
    pod = ready_pods[0]
    gpu = os.environ['GPU_RESOURCE_NAME']
    if not any(str(c.get('resources', {}).get('limits', {}).get(gpu)) == os.environ['GPU_COUNT'] for c in pod['spec']['containers']):
        raise RuntimeError('Predictor GPU allocation mismatch')
    endpoint = os.environ.get('INFERENCE_URL') or f'http://{model}-predictor.{NS}.svc.cluster.local:8080'

    # KServe can report Ready a few seconds before the cluster Service endpoint is
    # consistently reachable from another pod. Retry the API call until the same
    # readiness deadline instead of failing the PipelineRun on the first transient
    # connection timeout/refusal.
    def inference(path, body=None):
        url = endpoint.rstrip('/') + path
        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'Inference endpoint did not become reachable: {path}')
            req = urllib.request.Request(url,
                data=None if body is None else json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=min(30, max(1, remaining))) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code not in (408, 429, 500, 502, 503, 504):
                    raise
                detail = f'HTTP {error.code}'
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                detail = type(error).__name__
            print(f'Inference endpoint not ready ({path}, attempt {attempt}, {detail}); retrying', flush=True)
            time.sleep(min(10, max(1, deadline - time.monotonic())))

    listing = inference('/v1/models')
    if not any(m.get('id') == model for m in listing.get('data', [])):
        raise RuntimeError('Expected model absent from /v1/models')
    answer = inference('/v1/chat/completions', {'model': model, 'messages': [
        {'role': 'user', 'content': 'What is the capital of Ireland? Answer in one sentence.'}],
        'temperature': 0, 'max_tokens': 64})
    content = answer['choices'][0]['message']['content']
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError('Empty inference answer')
    print(json.dumps({'ready': True, 'startup_seconds': elapsed, 'pod': pod['metadata']['name'],
        'node': pod['spec']['nodeName'], 'gpu_resource': gpu, 'answer': content}), flush=True)
    for env, value in [('RESULT_READY', 'true'), ('RESULT_STARTUP', str(elapsed)),
                       ('RESULT_POD', pod['metadata']['name']), ('RESULT_NODE', pod['spec']['nodeName'])]:
        Path(os.environ[env]).write_text(value)


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        # Never print request headers or authentication response bodies.
        raise SystemExit(f'HTTP failure {error.code}; check resource status and operator logs')
