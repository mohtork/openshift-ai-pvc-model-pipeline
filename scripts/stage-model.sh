#!/bin/bash
set -euo pipefail
# Parameters are environment values, never interpolated into shell source.
[[ "$MODEL_DIR" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo 'Invalid model directory'; exit 1; }
test -s /auth/config.json || { echo 'Missing mounted registry auth file'; exit 1; }
mountpoint -q /podman-storage || { echo 'Staging PVC is not mounted'; exit 1; }
mountpoint -q /models || { echo 'Permanent PVC is not mounted'; exit 1; }
# The extraction Task has only the Podman outer step image. Query PVC status
# with curl rather than pulling a second utility image into this Task pod.
command -v curl >/dev/null || { echo 'Podman runtime image must include curl'; exit 1; }
[[ "$STAGING_PVC" =~ ^[a-z0-9][a-z0-9-]*$ && "$NAMESPACE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || exit 1
PVC_JSON=$(printf 'Authorization: Bearer %s\n' "$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" |
    curl --fail --silent --show-error --max-time 30 \
      --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
      --header @- "https://kubernetes.default.svc/api/v1/namespaces/$NAMESPACE/persistentvolumeclaims/$STAGING_PVC")
grep -Eq '"phase"[[:space:]]*:[[:space:]]*"Bound"' <<< "$PVC_JSON" || { echo 'RBD staging PVC not Bound'; exit 1; }
echo "Staging PVC $STAGING_PVC is Bound"
[[ ! -e "/models/$MODEL_DIR" && ! -L "/models/$MODEL_DIR" ]] || { echo 'Destination exists; refusing overwrite, including incomplete models'; exit 1; }
WRITE_TEST=$(mktemp /models/.write-test.XXXXXX)
ls -ln "$WRITE_TEST"
rm "$WRITE_TEST"
# Check capacity before downloading; bounds are admin-configured estimates.
for pair in "/podman-storage:$MIN_STAGING_GIB" "/models:$MIN_MODEL_GIB"; do
    path=${pair%:*}; minimum=${pair##*:}
    [[ "$minimum" =~ ^[1-9][0-9]*$ ]] || exit 1
    available=$(df -Pk "$path" | awk 'NR==2 {print $4}')
    (( available >= minimum * 1024 * 1024 )) || { echo "Insufficient free capacity on $path; require ${minimum}GiB"; exit 1; }
done
mkdir -p /podman-storage/{root,run,tmp}
export TMPDIR=/podman-storage/tmp
# Isolate configuration as well as flags: no inherited imagestore/additional stores.
export CONTAINERS_STORAGE_CONF=/podman-storage/storage.conf
cat > "$CONTAINERS_STORAGE_CONF" <<'CONF'
[storage]
driver = "overlay"
graphroot = "/podman-storage/root"
runroot = "/podman-storage/run"
[storage.options]
additionalimagestores = []
CONF
p() {
    podman --remote=false --root /podman-storage/root \
      --runroot /podman-storage/run --storage-driver=overlay \
      --tmpdir /podman-storage/tmp/libpod --events-backend=file \
      --cgroup-manager=cgroupfs "$@"
}
CID=''
cleanup() {
    status=$?
    trap - EXIT
    if [[ -n "$CID" ]]; then
        p rm "$CID" || echo 'WARNING: container metadata cleanup failed; retain staging for investigation'
    fi
    # Never remove destination files or staging layers on failure.
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
podman --version
df -h /podman-storage /models
p info
GRAPHROOT=$(p info --format '{{.Store.GraphRoot}}')
RUNROOT=$(p info --format '{{.Store.RunRoot}}')
COPYTMP=$(p info --format '{{.Store.ImageCopyTmpDir}}')
echo "GraphRoot=$GRAPHROOT RunRoot=$RUNROOT TMPDIR=$TMPDIR ImageCopyTmpDir=$COPYTMP"
[[ "$(realpath "$GRAPHROOT")" == /podman-storage/root ]] || { echo 'GraphRoot not on staging PVC'; exit 1; }
[[ "$(realpath "$RUNROOT")" == /podman-storage/run ]] || { echo 'RunRoot not on staging PVC'; exit 1; }
[[ "$TMPDIR" == /podman-storage/tmp && "$(realpath "$COPYTMP")" == /podman-storage/tmp ]] || { echo 'Download temporary storage not on staging PVC'; exit 1; }
# Atomic directory creation reserves the destination across concurrent runs.
# Failure leaves it intact for administrator inspection; no automatic refresh.
mkdir "/models/$MODEL_DIR"
p pull --authfile /auth/config.json "$MODEL_IMAGE"
p images
df -h /podman-storage
du -sh /podman-storage/* || true
# The ModelCar never starts. No Kubernetes container uses this image.
CID=$(p create --pull=never --network=none "$MODEL_IMAGE")
echo "Stopped container: $CID"
p ps -a
p cp "${CID}:/models/." "/models/${MODEL_DIR}/"
chmod -R a+rX "/models/$MODEL_DIR"
sync
test -s "/models/$MODEL_DIR/config.json"
[[ -s "/models/$MODEL_DIR/tokenizer.json" || -s "/models/$MODEL_DIR/tokenizer.model" ]] || { echo 'No nonempty tokenizer'; exit 1; }
WEIGHTS=$(find "/models/$MODEL_DIR" -maxdepth 1 -type f -name '*.safetensors' -size +0c -print)
[[ -n "$WEIGHTS" ]] || { echo 'No nonempty safetensors'; exit 1; }
du -sh "/models/$MODEL_DIR"
find "/models/$MODEL_DIR" -maxdepth 1 -type f -printf '%f\n' | sort
find "/models/$MODEL_DIR" -maxdepth 1 -name '*.safetensors' -printf '%f\n' | sort
ls -lah "/models/$MODEL_DIR"
# Results are written only after validation and successful metadata removal.
FINAL_CID="$CID"
CID=''
p rm "$FINAL_CID" || { echo 'WARNING: container metadata cleanup failed; retain staging for investigation'; exit 1; }
du -sh "/models/$MODEL_DIR" | awk '{printf "%s",$1}' > "$RESULT_SIZE"
printf true > "$RESULT_VALIDATED"
echo 'Extraction validated. Permanent model retained; staging PVC may be cleaned after Task termination.'
