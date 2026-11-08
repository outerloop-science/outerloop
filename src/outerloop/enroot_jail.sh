# Outerloop's enroot launcher (EnrootRuntime in container.py): runs one command
# in an enroot jail with the walls Apptainer's `exec --containall --cleanenv`
# gives, and removes everything it unpacked on every exit. container.py hands
# bash this file as text, so a job script that runs after the kernel checkout
# that wrote it has moved never reads it from disk:
#
#   bash -c "$(cat enroot_jail.sh)" outerloop-enroot [--enroot PROGRAM]
#       [--shared-ipc] [--shared-processes] [--gpus] --home DIR
#       [--bind SOURCE TARGET ro|rw]... [--pwd DIR] [--workdir DIR] IMAGE COMMAND...
#   bash -c "$(cat enroot_jail.sh)" outerloop-enroot --check [--enroot PROGRAM]
#       [--shared-ipc] [--shared-processes]
#
# ENROOTENV_<name> in this script's environment appears inside as <name>.
# Nothing else of the host environment crosses, and no value reaches a command
# line, which every user can read in /proc. The jail runs as the caller on a
# read-only copy of IMAGE, with no host home, a minimal /dev, private /tmp and
# /var/tmp (WORKDIR's tmp and var_tmp when given), HOME bound at its own path,
# then the binds in order. enroot 4.2 and later give it its own process and IPC
# namespaces (--pid, --ipc); where they cannot start, the jail runs only under
# the opt-in that accepts the gap. --check reports which jail this machine
# builds, with the host's own programs standing in for an image.

set -u
umask 077

die() {
    printf 'outerloop: %s\n' "$*" >&2
    exit 255
}

enroot=enroot check='' gpus='' home='' pwd='' workdir='' shared_ipc='' shared_processes=''
binds=()
while [ $# -gt 0 ]; do
    case $1 in
    --check) check=y; shift ;;
    --shared-ipc) shared_ipc=y; shift ;;
    --shared-processes) shared_processes=y; shift ;;
    --gpus) gpus=y; shift ;;
    --enroot | --home | --pwd | --workdir)
        [ $# -ge 2 ] || die "$1 needs a value"
        case $1 in
        --enroot) enroot=$2 ;;
        --home) home=$2 ;;
        --pwd) pwd=$2 ;;
        --workdir) workdir=$2 ;;
        esac
        shift 2
        ;;
    --bind)
        [ $# -ge 4 ] || die "--bind needs a source, a target and ro or rw"
        binds+=("$2" "$3" "$4")
        shift 4
        ;;
    *) break ;;
    esac
done

if [ -z "$check" ]; then
    [ $# -ge 2 ] || die "usage: outerloop-enroot [options] IMAGE COMMAND..."
    image=$1
    shift
    [ -f "$image" ] || die "no image at $image"
    mountable() {
        # enroot's mount table splits its lines on whitespace and decodes no escapes
        case $1 in
        *[[:space:]]*) die "enroot cannot mount a path that holds whitespace: $1" ;;
        /*) ;;
        *) die "a jail path must be absolute: $1" ;;
        esac
    }
    mountable "$home"
    [ -d "$home" ] || die "no home directory at $home"
    if [ -n "$workdir" ]; then
        mountable "$workdir"
        [ -d "$workdir" ] || die "no work directory at $workdir"
    fi
    for ((i = 0; i < ${#binds[@]}; i += 3)); do
        mountable "${binds[i]}"
        mountable "${binds[i + 1]}"
        case ${binds[i + 2]} in
        ro | rw) ;;
        *) die "a bind is ro or rw, not ${binds[i + 2]}" ;;
        esac
        [ -e "${binds[i]}" ] || die "no bind source at ${binds[i]}"
    done
fi

# What crosses into the jail, checked before anything else: enroot writes the
# assembled environment as one NAME=value line per variable and strips quotes
# that wrap a value, so a value holding a newline or wrapped in quotes would
# arrive changed (a newline could even add a variable).
quoted='^["'\''].*["'\''][[:space:]]*$'
for name in $(compgen -e); do
    case $name in
    ENROOTENV_*)
        inner=${name#ENROOTENV_}
        case $inner in
        HOME | ENROOT_* | NVIDIA_* | MELLANOX_*) die "refusing to forward $inner into the jail" ;;
        esac
        case ${!name} in
        *$'\n'*) die "$inner holds a newline, which enroot cannot pass into a jail" ;;
        esac
        [[ ${!name} =~ $quoted ]] && die "$inner is wrapped in quotes, which enroot strips"
        ;;
    esac
done

# An evaluation unpacks into its own work directory, a session into TMPDIR;
# enroot also writes this path into mount tables.
scratch_root=${workdir:-${TMPDIR:-/tmp}}
case $scratch_root in
*[[:space:]]*) die "enroot cannot unpack under a path that holds whitespace: $scratch_root" ;;
/*) ;;
*) die "the directory to unpack under must be absolute: $scratch_root" ;;
esac
dir=$(mktemp -d "$scratch_root/outerloop-enroot.XXXXXXXX") || die "cannot create a directory under $scratch_root"
cleanup() {
    rm -rf -- "$dir" 2>/dev/null || { chmod -R u+rwX -- "$dir" 2>/dev/null; rm -rf -- "$dir"; }
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP
# A jail killed outright (the harness kills a timed-out session's process group
# with SIGKILL) runs no trap, so a helper in its own session, holding none of
# this environment, removes the directory once every holder of this pipe is gone.
exec {holder}> >(exec env -i PATH="$PATH" setsid sh -c \
    'cat >/dev/null; rm -rf -- "$1" 2>/dev/null || { chmod -R u+rwX -- "$1"; rm -rf -- "$1"; }' \
    outerloop-enroot-cleanup "$dir" >/dev/null 2>&1)

# enroot reads ENROOT_* settings and its hooks read NVIDIA_*, MELLANOX_*, SLURM_*
# and PMIX_* from its own environment, so it starts from one this script sets:
# only PATH, the forwarded variables and enroot's directories, which all lie in
# this run's scratch directory. The site's configuration (/etc/enroot) applies;
# the caller's own (~/.config/enroot) does not.
for name in $(compgen -e); do
    case $name in
    PATH | ENROOTENV_*) ;;
    # an exported bash function (BASH_FUNC_x%%, e.g. the module command on HPC) is
    # not a variable enroot reads as a setting, and unset -v / export -n cannot clear
    # it; leaving it is harmless (enroot reads only ENROOT_*-shaped names)
    [!A-Za-z_]* | *[!A-Za-z0-9_]*) ;;
    *) unset -v "$name" 2>/dev/null || export -n "$name" || die "cannot clear $name" ;;
    esac
done
mkdir -p "$dir/run" "$dir/data" "$dir/cache" "$dir/config" "$dir/tmp"
export ENROOT_RUNTIME_PATH="$dir/run" ENROOT_DATA_PATH="$dir/data" ENROOT_CACHE_PATH="$dir/cache" \
    ENROOT_CONFIG_PATH="$dir/config" ENROOT_TEMP_PATH="$dir/tmp" TMPDIR="$dir/tmp" \
    NVIDIA_VISIBLE_DEVICES=void MELLANOX_VISIBLE_DEVICES=void

version=$("$enroot" version </dev/null 2>/dev/null) || die "cannot run $enroot"
case $version in
[0-3].*) die "enroot $version is older than 4.0" ;;
esac

write_conf() {
    # The settings and functions enroot reads from --conf: $1 is the file, $2
    # is "gpus" for a jail with GPUs, and the rest are lines for mounts().
    local file=$1 devices=$2 node
    shift 2
    {
        printf '%s\n' '#ENROOT_REMAP_ROOT=no' '#ENROOT_ROOTFS_WRITABLE=no' '#ENROOT_MOUNT_HOME=no' \
            '#ENROOT_RESTRICT_DEV=no' '#ENROOT_LOGIN_SHELL=no' '#ENROOT_ALLOW_SUPERUSER=no' \
            '#ENROOT_UNSHARE_PID=no' '#ENROOT_UNSHARE_IPC=no' '#ENROOT_UNSHARE_NET=no' \
            '#ENROOT_UNSHARE_UTS=no'
        cat <<'EOF'
environ() {
    local name
    for name in $(compgen -e ENROOTENV_); do
        printf '%s=%s\n' "${name#ENROOTENV_}" "${!name}"
    done
}

hooks() {
    enroot-mount --root "${ENROOT_ROOTFS}" - <<'DEV'
none /dev none x-detach,nofail,silent
tmpfs /dev tmpfs x-create=dir,rw,nosuid,noexec,mode=755,slave
/dev/null /dev/null none x-create=file,bind,rw,nosuid,noexec,private
/dev/zero /dev/zero none x-create=file,bind,rw,nosuid,noexec,private
/dev/full /dev/full none x-create=file,bind,rw,nosuid,noexec,private
/dev/random /dev/random none x-create=file,bind,rw,nosuid,noexec,private
/dev/urandom /dev/urandom none x-create=file,bind,rw,nosuid,noexec,private
/dev/tty /dev/tty none x-create=file,bind,rw,nosuid,noexec,private
devpts /dev/pts devpts x-create=dir,rw,nosuid,noexec,newinstance,ptmxmode=0666,mode=620,slave
tmpfs /dev/shm tmpfs x-create=dir,rw,nosuid,nodev,noexec,mode=1777,slave
DEV
EOF
        if [ "$devices" = gpus ]; then
            # the GPU device files Apptainer's --nv binds, back over the fresh /dev
            echo "    enroot-mount --root \"\${ENROOT_ROOTFS}\" - <<'GPU'"
            for node in /dev/nvidia*; do
                if [ -d "$node" ]; then
                    echo "$node $node none x-create=dir,rbind,rw,nosuid,noexec,private"
                elif [ -c "$node" ]; then
                    echo "$node $node none x-create=file,bind,rw,nosuid,noexec,private"
                fi
            done
            echo GPU
        fi
        cat <<'EOF'
    ln -s /proc/self/fd "${ENROOT_ROOTFS}/dev/fd"
    ln -s /proc/self/fd/0 "${ENROOT_ROOTFS}/dev/stdin"
    ln -s /proc/self/fd/1 "${ENROOT_ROOTFS}/dev/stdout"
    ln -s /proc/self/fd/2 "${ENROOT_ROOTFS}/dev/stderr"
    ln -s pts/ptmx "${ENROOT_ROOTFS}/dev/ptmx"
}

mounts() {
    cat <<'FSTAB'
EOF
        printf '%s\n' "$@"
        cat <<'EOF'
FSTAB
}

rc() {
    cd -- "$1" || exit 1
    shift
    if [ "$$" != 1 ]; then
        exec "$@"
    fi
    child='' pending=''
    trap 'pending=TERM; [ -z "$child" ] || kill -TERM "$child" 2>/dev/null' TERM
    trap 'pending=INT; [ -z "$child" ] || kill -INT "$child" 2>/dev/null' INT
    trap 'pending=HUP; [ -z "$child" ] || kill -HUP "$child" 2>/dev/null' HUP
    exec 3<&0
    "$@" <&3 3<&- &
    child=$!
    exec 3<&-
    [ -z "$pending" ] || kill -"$pending" "$child" 2>/dev/null
    wait "$child"
    status=$?
    while kill -0 "$child" 2>/dev/null; do
        wait "$child"
        status=$?
    done
    exit "$status"
}
EOF
    } >"$file"
}

if [ -n "$check" ]; then
    # the host's own programs, read-only, stand in for an image
    mkdir -p "$dir/data/jail/etc"
    : >"$dir/data/jail/etc/passwd"
    : >"$dir/data/jail/etc/group"
    host=()
    for path in /bin /lib /lib64 /sbin /usr; do
        [ -d "$path" ] && host+=("$path $path none x-create=dir,rbind,ro,nosuid,nodev")
    done
    write_conf "$dir/probe.conf" none ${host[@]+"${host[@]}"}
elif ! "$enroot" create -n jail "$image" </dev/null >"$dir/create.log" 2>&1; then
    cat "$dir/create.log" >&2
    die "enroot could not unpack $image"
else
    write_conf "$dir/probe.conf" none
fi

# Which namespaces enroot gives a jail here, seen from inside one: a level
# counts only when its jail starts and has the walls the level promises.
host_pid=$(readlink /proc/self/ns/pid) host_ipc=$(readlink /proc/self/ns/ipc)
probe() {
    local want=$1
    shift
    "$enroot" start "$@" --conf "$dir/probe.conf" jail / /bin/sh -c '
        p=$(readlink /proc/self/ns/pid); [ -n "$p" ] && [ "$p" != "$1" ] || exit 1
        [ "$3" = pid ] && exit 0
        i=$(readlink /proc/self/ns/ipc); [ -n "$i" ] && [ "$i" != "$2" ]' \
        probe "$host_pid" "$host_ipc" "$want" </dev/null >/dev/null 2>>"$dir/probe.log"
}
if probe all --pid --ipc; then
    level=full flags=(--pid --ipc)
elif [ -n "$shared_ipc$shared_processes" ] && probe pid --pid; then
    level=shared-ipc flags=(--pid)
elif [ -n "$shared_processes" ]; then
    level=shared-processes flags=()
else
    case $version in
    4.[01].*) why="enroot $version predates --pid and --ipc, which arrived in 4.2" ;;
    *) why=$(tail -n 1 "$dir/probe.log") ;;
    esac
    die "enroot cannot give this jail its own process and IPC namespaces here ($why);" \
        "OUTERLOOP_ENROOT_SHARED_IPC=1 accepts a shared IPC namespace," \
        "OUTERLOOP_ENROOT_SHARED_PROCESSES=1 a shared process view as well"
fi

if [ -n "$check" ]; then
    if [ "$level" = shared-processes ] && ! "$enroot" start --conf "$dir/probe.conf" jail / /bin/true \
        </dev/null >/dev/null 2>>"$dir/probe.log"; then
        die "enroot cannot start a container here ($(tail -n 1 "$dir/probe.log"))"
    fi
    echo "$level"
    exit 0
fi
case $level in
shared-ipc) echo "outerloop: this jail shares the host's IPC namespace (OUTERLOOP_ENROOT_SHARED_IPC=1)" >&2 ;;
shared-processes) echo "outerloop: this jail shares the host's process and IPC namespaces (OUTERLOOP_ENROOT_SHARED_PROCESSES=1)" >&2 ;;
esac

# /tmp and /var/tmp first (WORKDIR's, or fresh in-memory ones), then the home,
# then the binds in order, so a home or bind under /tmp shows over it.
if [ -n "$workdir" ]; then
    mkdir -p "$workdir/tmp" "$workdir/var_tmp"
    lines=("$workdir/tmp /tmp none x-create=dir,bind,rw,nosuid,nodev"
        "$workdir/var_tmp /var/tmp none x-create=dir,bind,rw,nosuid,nodev")
else
    lines=("tmpfs /tmp tmpfs x-create=dir,rw,nosuid,nodev,mode=1777"
        "tmpfs /var/tmp tmpfs x-create=dir,rw,nosuid,nodev,mode=1777")
fi
lines+=("$home $home none x-create=dir,bind,rw,nosuid,nodev")
for ((i = 0; i < ${#binds[@]}; i += 3)); do
    kind='file'
    [ -d "${binds[i]}" ] && kind=dir
    lines+=("${binds[i]} ${binds[i + 1]} none x-create=$kind,bind,${binds[i + 2]},nosuid,nodev")
done
if [ -n "$gpus" ]; then
    export NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
    write_conf "$dir/jail.conf" gpus "${lines[@]}"
else
    write_conf "$dir/jail.conf" none "${lines[@]}"
fi

# enroot runs as a child so this script can remove the directory afterwards;
# the termination signals this script receives go on to it, and the script
# leaves with the command's status. enroot runs in a subshell that first closes
# the cleanup pipe (the {holder} fd is not close-on-exec), so the jailed, untrusted
# command never inherits it: otherwise it could hold the write end open past a
# SIGKILL of this script and leak the unpacked tree, or read the host fd.
exec 3<&0
(
    exec {holder}>&-
    exec "$enroot" start ${flags[@]+"${flags[@]}"} --conf "$dir/jail.conf" --env "HOME=$home" jail \
        "${pwd:-$home}" "$@" <&3 3<&-
) &
child=$!
exec 3<&-
trap 'kill -TERM "$child" 2>/dev/null' TERM
trap 'kill -INT "$child" 2>/dev/null' INT
trap 'kill -HUP "$child" 2>/dev/null' HUP
wait "$child"
status=$?
while kill -0 "$child" 2>/dev/null; do
    wait "$child"
    status=$?
done
exit "$status"
