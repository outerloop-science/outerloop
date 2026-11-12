# containers

`agent-py312.def` is the recipe for the image the kernel runs sessions and
evaluations in. The `build-image` workflow builds it on every change to this
directory and on manual dispatch, for both runtimes and both processors from
this one recipe: a `.sif` for Apptainer and a `.sqsh` for enroot, each for
x86-64 and ARM64. It publishes the results to
[huggingface.co/outerloop-science/agent-image](https://huggingface.co/outerloop-science/agent-image)
with a checksum each. `outerloop init` downloads the one for the machine and
runtime on Linux when the container runtime works; the tick reads
`~/outerloop-images/agent-py312.sif` (`.sqsh` under enroot) unless
`OUTERLOOP_IMAGE` says otherwise.
