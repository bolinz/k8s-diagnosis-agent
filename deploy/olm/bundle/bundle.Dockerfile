FROM scratch

# Core metadata labels required by OLM
LABEL operators.operatorframework.io.bundle.channel.default.v1=stable
LABEL operators.operatorframework.io.bundle.channels.v1=stable,alpha
LABEL operators.operatorframework.io.bundle.manifests.v1=manifests/
LABEL operators.operatorframework.io.bundle.metadata.v1=metadata/
LABEL operators.operatorframework.io.bundle.package.v1=k8s-diagnosis-agent
LABEL operators.operatorframework.io.bundle.version.v1=0.7.0
LABEL operators.operatorframework.io.metrics.builder=operator-sdk-v1.36.1
LABEL operators.operatorframework.io.metrics.mediatype.v1=metrics/v1
LABEL operators.operatorframework.io.metrics.project_layout=helm.sdk.operatorframework.io/v1

# Copy manifests
COPY deploy/olm/bundle/manifests /manifests/

# Copy metadata
COPY deploy/olm/bundle/metadata /metadata/

# Copy README for bundle validation context
COPY README.md /README.md
