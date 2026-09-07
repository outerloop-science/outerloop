# containers

`agent-py312.def` is the Apptainer recipe for the image the kernel runs sessions and
evaluations in. The `build-image` workflow builds it on every change to this directory and
on manual dispatch, and publishes the result to
[huggingface.co/outerloop-science/agent-image](https://huggingface.co/outerloop-science/agent-image)
with a checksum. `outerloop init` downloads it on Linux when Apptainer is installed; the
tick reads `~/outerloop-images/agent-py312.sif` unless `OUTERLOOP_IMAGE` says otherwise.
