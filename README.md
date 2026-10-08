# OpenShift AI ModelCar -> CephFS -> KServe Automation

This is the reduced package for the validated POC. It contains only the files required to prepare and run the administrator-operated Tekton workflow that:

1. creates a permanent ODF CephFS model PVC;
2. creates a temporary ODF RBD workspace PVC;
3. runs Podman with `graphroot`, `runroot`, and temporary pull data on the RBD PVC;
4. pulls the approved Red Hat ModelCar without using node-local CRI-O storage for the ModelCar;
5. extracts `/models` into the permanent CephFS PVC;
6. deploys KServe/vLLM using `pvc://<pvc>/<model-dir>/`;
7. waits for the predictor, verifies GPU allocation, calls `/v1/models`, and runs a chat-completion test;
8. optionally collects evidence, verifies node CRI-O image inventory, and removes the temporary staging PVC.

The permanent CephFS PVC is retained.

## Validated POC values

The POC was validated with:

```text
Model:              llama-3-1-8b-instruct
ModelCar:           registry.redhat.io/rhelai1/modelcar-llama-3-1-8b-instruct-quantized-w4a16:1.5
RBD StorageClass:   ocs-storagecluster-ceph-rbd
CephFS StorageClass:ocs-storagecluster-cephfs
Permanent PVC:      model-storage
Model directory:    llama-3-1-8b-instruct
ServingRuntime:     vllm-runtime
GPU resource:       nvidia.com/mig-3g.40gb
GPU count:          1
CPU:                4
Memory:             16Gi
Model PVC size:     20Gi
Staging PVC size:   20Gi
```

The final vLLM API returned a successful chat completion and `/v1/models` reported `root=/mnt/models`.

## Prerequisites

Run this workflow as an OpenShift/platform administrator. The cluster must already have:

- OpenShift Pipelines / Tekton;
- OpenShift AI / KServe CRDs;
- ODF RBD and CephFS StorageClasses;
- NVIDIA GPU/MIG resources;
- access to `registry.redhat.io` through the OpenShift pull secret;
- Python 3, PyYAML, and the `oc` CLI on the administration host.

The helper does **not** install operators.

Confirm the storage classes first:

```bash
oc get sc
```

## Files

```text
README.md
runtime/
  vllm-runtime.yaml
scripts/
  admin-lab.sh
  admin_lab.py
  stage-model.sh
  task-api.py
storage/
  model-pvc.yaml
tekton/
  approved-models.yaml
  pipeline.yaml
  pipelinerun-lab.yaml
  task-validate-environment.yaml
  task-podman-stage-model.yaml
  task-deploy-model.yaml
  task-test-model.yaml
  rbac/
    serviceaccount.yaml
    role.yaml
    rolebinding.yaml
    extractor-serviceaccount.yaml
    extractor-role.yaml
    extractor-rolebinding.yaml
```

`task-api.py` and `stage-model.sh` are kept as readable source copies of the logic embedded in the Tekton Task manifests.

## 1. Set the deployment variables

For a new test project:

```bash
export NS=rag-ceph-lab
export RBD_STORAGE_CLASS=ocs-storagecluster-ceph-rbd
export CEPHFS_STORAGE_CLASS=ocs-storagecluster-cephfs
export RUNTIME_MANIFEST="$PWD/runtime/vllm-runtime.yaml"
export SERVING_RUNTIME=vllm-runtime

export GPU_RESOURCE_NAME=nvidia.com/mig-3g.40gb
export GPU_COUNT=1
export CPU_REQUEST=4
export MEMORY_REQUEST=16Gi

export MODEL_SIZE=20Gi
export STAGING_SIZE=20Gi
```

For an existing researcher project, use its namespace and explicitly allow the helper to use it:

```bash
export NS=<researcher-project>
export USE_EXISTING_NAMESPACE=yes
```

Safety behavior: if the existing namespace already contains `model-storage` or the `podman-model-extractor` service account, `prepare` refuses to overwrite them.

## 2. Prepare the namespace and pipeline

```bash
bash scripts/admin-lab.sh prepare
```

The helper performs preflight checks, creates or uses the namespace, creates the permanent CephFS PVC, creates the Tekton service accounts/RBAC, copies registry credentials into the temporary project secret, grants the `privileged` SCC only to `podman-model-extractor`, installs the approved Tasks/Pipeline and ServingRuntime, and renders the PipelineRun.

Confirm the permanent PVC:

```bash
oc get pvc -n "$NS"
```

Expected permanent PVC:

```text
model-storage   Bound   ...   RWX   ocs-storagecluster-cephfs
```

## 3. Start the PipelineRun

```bash
bash scripts/admin-lab.sh start
```

The command prints a generated run name similar to:

```text
RUN=ceph-model-xxxxx
```

The helper also saves the run name under `customer-test/<namespace>/run-name.txt`.

If working from another shell, set it explicitly:

```bash
export RUN=<pipeline-run-name>
```

## 4. Monitor the run

Use the helper:

```bash
bash scripts/admin-lab.sh watch
```

For direct Tekton status:

```bash
oc get pipelineruns.tekton.dev -n "$NS"
oc get taskruns.tekton.dev -n "$NS"
```

Watch continuously:

```bash
watch -n 5 "oc get pipelineruns.tekton.dev,taskruns.tekton.dev,pods -n $NS"
```

The expected task order is:

```text
validate-environment -> extract-model -> deploy-model -> wait-and-test
```

Monitor a specific TaskRun:

```bash
oc get taskruns.tekton.dev -n "$NS"
oc logs -n "$NS" <taskrun-pod> --all-containers -f
```

To find all Tekton pods belonging to the current run:

```bash
oc get pods -n "$NS" -l tekton.dev/pipelineRun="$RUN"
```

### Monitor extraction

During `extract-model`, the logs should show Podman using the PVC-backed paths:

```text
GraphRoot=/podman-storage/root
RunRoot=/podman-storage/run
TMPDIR=/podman-storage/tmp
ImageCopyTmpDir=/podman-storage/tmp
```

Then confirm the model was copied to the permanent PVC and validation succeeded.

### Monitor KServe/vLLM

```bash
oc get inferenceservices.serving.kserve.io -n "$NS"
oc get pods -n "$NS" -l serving.kserve.io/inferenceservice=llama-3-1-8b-instruct -o wide
```

The InferenceService should eventually show `READY=True`.

Check vLLM logs:

```bash
POD=$(oc get pod -n "$NS" -l serving.kserve.io/inferenceservice=llama-3-1-8b-instruct -o jsonpath='{.items[0].metadata.name}')
oc logs -n "$NS" "$POD" -c kserve-container --tail=200
```

Useful success indicators include vLLM loading `/mnt/models`, starting the API server on port 8080, and completing application startup.

## 5. Final validation behavior - fixed

The original POC reached `InferenceService Ready=True`, but the Tekton `wait-and-test` step made its first HTTP request during a short service/network convergence window and failed with:

```text
urllib.error.URLError: <urlopen error [Errno 110] Connection timed out>
```

The model itself was healthy; manual `/v1/models` and `/v1/chat/completions` calls succeeded immediately afterward.

This package fixes that behavior. `task-test-model.yaml` now retries transient inference connectivity errors after KServe reports Ready instead of failing the PipelineRun on the first timeout. It retries until the configured `READY_TIMEOUT_SECONDS` deadline. Retryable conditions include connection/URL timeouts and HTTP 408/429/500/502/503/504.

You may see log lines such as:

```text
Inference endpoint not ready (/v1/models, attempt 1, URLError); retrying
```

That is expected during convergence. The task succeeds only after both of these work:

```text
GET  /v1/models
POST /v1/chat/completions
```

## 6. Check the pipeline results

After success:

```bash
oc get pipelinerun "$RUN" -n "$NS" -o jsonpath='{.status.conditions[0]}{"\n"}'
```

Print results:

```bash
oc get pipelinerun "$RUN" -n "$NS" -o json | jq '.status.results'
```

Expected result names include:

```text
MODEL_SIZE
MODEL_VALIDATED
READY
STARTUP_SECONDS
PREDICTOR_POD
GPU_NODE
```

Typical successful values include `MODEL_VALIDATED=true` and `READY=true`.

## 7. Validate the model directly inside the cluster

Get the predictor pod:

```bash
POD=$(oc get pod -n "$NS" -l serving.kserve.io/inferenceservice=llama-3-1-8b-instruct -o jsonpath='{.items[0].metadata.name}')
```

Confirm the mounted model files:

```bash
oc exec -n "$NS" "$POD" -- ls -lh /mnt/models
oc exec -n "$NS" "$POD" -- df -h /mnt/models
```

Confirm vLLM reports the PVC-backed model root:

```bash
oc exec -n "$NS" "$POD" -- curl -s http://127.0.0.1:8080/v1/models
```

The response should report:

```text
"root":"/mnt/models"
```

Run an inference test:

```bash
oc exec -n "$NS" "$POD" -- curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"llama-3-1-8b-instruct",
    "messages":[{"role":"user","content":"What is the capital of Ireland? Answer in one sentence."}],
    "max_tokens":50,
    "temperature":0
  }'
```

The validated POC returned:

```text
The capital of Ireland is Dublin.
```

## 8. Collect evidence

```bash
bash scripts/admin-lab.sh evidence
```

Evidence is written under:

```text
customer-test/<namespace>/evidence/<run>/
```

Secrets are deliberately excluded.

## 9. Verify the ModelCar is absent from node CRI-O storage

Only after the PipelineRun succeeds:

```bash
bash scripts/admin-lab.sh verify-node
```

The check inspects the GPU node and the extraction node. It fails if a `modelcar` reference is found in CRI-O and verifies that the vLLM serving runtime image exists on the GPU node.

## 10. Remove temporary staging storage

After extraction is validated and evidence has been collected:

```bash
bash scripts/admin-lab.sh cleanup-staging
```

The helper deletes only the temporary RBD workspace PVC owned by this PipelineRun. It refuses to delete it unless extraction completed and returned `MODEL_VALIDATED=true`.

The permanent CephFS PVC is retained.

## Troubleshooting

### Pipeline failed

```bash
oc get taskruns.tekton.dev -n "$NS"
oc get events -n "$NS" --sort-by='.lastTimestamp' | tail -50
```

Find the failed TaskRun and inspect its pod:

```bash
oc get taskrun <taskrun-name> -n "$NS" -o jsonpath='{.status.podName}{"\n"}'
oc logs -n "$NS" <pod-name> --all-containers
```

### Extraction issue

Do **not** delete the staging PVC before diagnosis. Collect evidence first.

```bash
bash scripts/admin-lab.sh evidence
```

### KServe is Ready but API test is retrying

Check the internal service and predictor:

```bash
oc get svc,endpoints -n "$NS" | grep llama
oc get inferenceservices.serving.kserve.io -n "$NS"
oc get pods -n "$NS" -l serving.kserve.io/inferenceservice=llama-3-1-8b-instruct -o wide
```

The retry fix is specifically designed to tolerate a short delay between KServe readiness and reliable service connectivity.

### Re-running the same model

The workflow intentionally refuses to overwrite an existing InferenceService or existing permanent model PVC. For a clean full test, use a new namespace, or explicitly remove/review previous test resources first. Never delete the permanent model PVC unless you intentionally want to remove the extracted model.

## Researcher project use

To stage and deploy the model inside an existing researcher project, the administrator runs the same workflow with:

```bash
export NS=<researcher-project>
export USE_EXISTING_NAMESPACE=yes
```

The permanent CephFS PVC, ServingRuntime, and InferenceService then live in that researcher namespace. The privileged extraction identity is used only by the administrative staging task; the predictor itself does not run as that privileged service account.

External route/token configuration is intentionally outside this reduced package. It can be configured separately after the model deployment is validated.
