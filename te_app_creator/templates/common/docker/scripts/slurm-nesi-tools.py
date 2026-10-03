

# ---------------------------------------------------------------------------
# Added by training-environment-app-creator (docker/scripts/slurm-nesi-tools.py)
#
# NeSI's own seff and svisit, from nesi/opt-nesi-bin, run in this image as
# they run on the cluster. They talk to Slurm, so the emulator's commands learn
# the three things those tools ask of it:
#
#   sacct --json            the job records seff reads, in Slurm's JSON layout
#   squeue --Format=...     the job ids svisit looks for
#   srun --jobid=... --pty  svisit's way into a running job: the command runs
#                           with that job's environment, GPUs included
#
# Anything else still goes to the emulator's own implementation.
# ---------------------------------------------------------------------------

_emulator_sacct = sacct
_emulator_squeue = squeue
_emulator_srun = srun


def _ac_job_ids(text):
    """'1000,1001_2,1002.batch' -> {1000, 1001, 1002}"""
    ids = set()
    for part in str(text).split(","):
        base = part.strip().split(".")[0].split("_")[0]
        if base.isdigit():
            ids.add(int(base))
    return ids


def _ac_number(value):
    return {"set": True, "infinite": False, "number": int(value)}


def _ac_tres(kind, count, name=""):
    return {"type": kind, "name": name, "id": 0, "count": int(count), "task": 0, "node": NODE_NAME}


def _ac_gpu_kinds(job):
    """The card type of each GPU the job was given, as seff names them."""
    gres = node_gres()
    kinds = [gres[i] for i in job.gpu_ids if i < len(gres)]
    if not kinds and job.gpus:
        kinds = [job.gpu_type or (gres[0] if gres else "gpu")] * job.gpus
    return kinds


def _ac_job_json(job):
    """One job, with the fields of Slurm's sacct --json that seff reads."""
    started = bool(job.start_time) and job.state != PENDING
    start = int(job.start_time)
    # a step that starts and ends in the same second has no events for seff to
    # measure, so every finished step lasts at least a second
    end = max(int(job.end_time or time.time()), start + 1) if started else 0
    cluster = os.environ.get("GPUEMU_CLUSTER", "training")
    partition = job.partition
    kinds = _ac_gpu_kinds(job) if job.gpus else []
    # seff sizes GPUs from a per-partition table, where the 40 GB A100 is the
    # a100 of the genoa partition
    if kinds and all(kind == "a100_40" for kind in kinds):
        partition = "genoa"
    kinds = ["a100" if kind == "a100_40" else kind for kind in kinds]

    allocated = []
    steps = []
    if started:
        allocated = [_ac_tres("cpu", job.cpus), _ac_tres("mem", job.mem_mb), _ac_tres("node", 1)]
        if kinds:
            allocated.append(_ac_tres("gres", len(kinds), "gpu"))
            for kind in sorted(set(kinds)):
                allocated.append(_ac_tres("gres", kinds.count(kind), f"gpu:{kind}"))
        memory = [_ac_tres("mem", job.max_rss_mb * 1024 * 1024)]
        total = [_ac_tres("cpu", job.cpu_seconds * 1000)] + memory
        if kinds:
            total.append(_ac_tres("gres", round(job.mean_gpu_util), "gpuutil"))
            total.append(_ac_tres("gres", job.gpu_mem_peak_mb * 1024 * 1024, "gpumem"))
        steps.append(
            {
                "step": {"id": f"{job.job_id}.batch", "name": "batch"},
                "state": [job.state],
                "time": {"start": _ac_number(start), "end": _ac_number(end), "elapsed": end - start},
                "tasks": {"count": 1},
                "nodes": {"count": 1, "list": [NODE_NAME]},
                "exit_code": {"return_code": _ac_number(job.exit_code)},
                "tres": {
                    "requested": {"total": total, "min": memory, "max": memory, "average": memory},
                    "consumed": {"total": total, "min": memory, "max": memory, "average": memory},
                    "allocated": allocated,
                },
            }
        )

    return {
        "job_id": job.job_id,
        "name": job.name,
        "user": job.user,
        "account": job.account or "default",
        "cluster": cluster,
        "partition": partition,
        "association": {"user": job.user, "account": job.account or "default", "cluster": cluster, "partition": partition},
        "state": {"current": [job.state], "reason": job.reason},
        "exit_code": {"status": ["SUCCESS" if job.exit_code == 0 else "ERROR"], "return_code": _ac_number(job.exit_code)},
        "array": {"job_id": 0, "task_id": {"set": False, "infinite": False, "number": 0}, "task": ""},
        "allocation_nodes": 1 if started else 0,
        "nodes": NODE_NAME if started else "None assigned",
        "working_directory": job.workdir,
        "time": {
            "submission": int(job.submit_time),
            "start": start,
            "end": end,
            "elapsed": int(job.elapsed),
            "limit": _ac_number(max(1, round(job.time_limit_s / 60))),
        },
        "required": {"CPUs": job.cpus, "memory_per_node": _ac_number(job.mem_mb)},
        "tres": {"allocated": allocated, "requested": allocated},
        "steps": steps,
    }


def _ac_drop_clusters(argv):
    """Remove -M/--clusters and -a/--allusers, which mean nothing on one node."""
    out = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("-M", "--clusters"):
            skip = True
            continue
        if arg.startswith("--clusters=") or (arg.startswith("-M") and len(arg) > 2) or arg in ("-a", "--allusers"):
            continue
        out.append(arg)
    return out


def sacct(argv=None):
    argv = _ac_drop_clusters(list(sys.argv[1:] if argv is None else argv))
    if not any(arg == "--json" or arg.startswith("--json=") for arg in argv):
        return _emulator_sacct(argv)
    ids, user = None, None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-j", "--jobs") and i + 1 < len(argv):
            ids = _ac_job_ids(argv[i + 1])
            i += 1
        elif arg.startswith("--jobs="):
            ids = _ac_job_ids(arg.split("=", 1)[1])
        elif arg.startswith("-j") and len(arg) > 2:
            ids = _ac_job_ids(arg[2:])
        elif arg in ("-u", "--user") and i + 1 < len(argv):
            user = argv[i + 1]
            i += 1
        i += 1
    jobs = JobStore().all()
    if ids is not None:
        jobs = [j for j in jobs if j.job_id in ids]
    if user:
        jobs = [j for j in jobs if j.user == user]
    document = {
        "meta": {"plugin": {"type": "openapi/slurmdbd", "data_parser": "data_parser/v0.0.43"}},
        "jobs": [_ac_job_json(j) for j in jobs],
        "warnings": [],
        "errors": [],
    }
    print(json.dumps(document, indent=2))
    return 0


_AC_FIELDS = {
    "jobid": ("JOBID", lambda j: str(j.job_id)),
    "jobarrayid": ("JOBID", lambda j: str(j.job_id)),
    "name": ("NAME", lambda j: j.name),
    "username": ("USER", lambda j: j.user),
    "account": ("ACCOUNT", lambda j: j.account or "default"),
    "partition": ("PARTITION", lambda j: j.partition),
    "state": ("STATE", lambda j: j.state),
    "statecompact": ("ST", lambda j: _STATE_ABBREV.get(j.state, j.state[:2])),
    "timeused": ("TIME", lambda j: format_duration(j.elapsed)),
    "timelimit": ("TIME_LIMIT", lambda j: format_duration(j.time_limit_s)),
    "numcpus": ("CPUS", lambda j: str(j.cpus)),
    "nodelist": ("NODELIST", lambda j: NODE_NAME if j.state == RUNNING else ""),
    "reason": ("REASON", lambda j: j.reason),
}


def _ac_squeue_formatted(fmt, argv):
    ap = argparse.ArgumentParser(prog="squeue", add_help=False)
    ap.add_argument("-u", "--user", default=None)
    ap.add_argument("-j", "--jobs", default=None)
    ap.add_argument("-p", "--partition", default=None)
    ap.add_argument("-t", "--states", default=None)
    ap.add_argument("-h", "--noheader", action="store_true")
    ap.add_argument("--me", action="store_true")
    args, _ = ap.parse_known_args(argv)

    columns = []
    for spec in fmt.split(","):
        name, _, width = spec.strip().partition(":")
        key = name.strip().lower()
        if key not in _AC_FIELDS:
            print(f"squeue: error: Invalid job format specification: {name}", file=sys.stderr)
            return 1
        digits = "".join(c for c in width if c.isdigit())
        columns.append((_AC_FIELDS[key], int(digits) if digits else 20))

    jobs = JobStore().all()
    if args.states:
        wanted = set()
        for state in args.states.split(","):
            state = state.strip().upper()
            wanted.add(state)
            wanted.update(full for full, abbrev in _STATE_ABBREV.items() if abbrev == state)
        jobs = [j for j in jobs if j.state in wanted]
    else:
        jobs = [j for j in jobs if j.state in ACTIVE_STATES]
    user = _current_user() if args.me else args.user
    if user:
        jobs = [j for j in jobs if j.user == user]
    if args.partition:
        jobs = [j for j in jobs if j.partition == args.partition]
    if args.jobs:
        ids = _ac_job_ids(args.jobs)
        jobs = [j for j in jobs if j.job_id in ids]

    if not args.noheader:
        print("".join(header.ljust(width) for (header, _), width in columns).rstrip())
    for job in jobs:
        print("".join(value(job)[:width].ljust(width) for (_, value), width in columns).rstrip())
    return 0


def squeue(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt, node, rest = None, None, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-O", "--Format") and i + 1 < len(argv):
            fmt = argv[i + 1]
            i += 1
        elif arg.startswith("--Format="):
            fmt = arg.split("=", 1)[1]
        elif arg.startswith("-O") and len(arg) > 2:
            fmt = arg[2:]
        elif arg in ("-w", "--nodelist") and i + 1 < len(argv):
            node = argv[i + 1]
            i += 1
        elif arg.startswith("--nodelist="):
            node = arg.split("=", 1)[1]
        elif arg.startswith("-w") and len(arg) > 2:
            node = arg[2:]
        else:
            rest.append(arg)
        i += 1
    if node is not None and node != NODE_NAME:
        # every job runs on this one node, so none runs on any other
        rest = rest + ["--states", "NONE"]
    if fmt is not None:
        return _ac_squeue_formatted(fmt, rest)
    return _emulator_squeue(rest)


def srun(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(arg == "--jobid" or arg.startswith("--jobid=") for arg in argv):
        return _emulator_srun(argv)

    job_id, node = None, None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--jobid" and i + 1 < len(argv):
            job_id = argv[i + 1]
            i += 1
        elif arg.startswith("--jobid="):
            job_id = arg.split("=", 1)[1]
        elif arg in ("-w", "--nodelist") and i + 1 < len(argv):
            node = argv[i + 1]
            i += 1
        elif arg.startswith("--nodelist="):
            node = arg.split("=", 1)[1]
        elif arg.startswith("-w") and len(arg) > 2:
            node = arg[2:]
        elif not arg.startswith("-"):
            break
        # --pty, --overlap and other options: one node, one terminal, nothing to do
        i += 1
    command = argv[i:] or [os.environ.get("SHELL", "/bin/bash")]

    ids = _ac_job_ids(job_id or "")
    job = JobStore().load(min(ids)) if ids else None
    if job is None:
        print(f"srun: error: Unable to confirm allocation for job {job_id}: Invalid job id specified", file=sys.stderr)
        return 1
    if job.state != RUNNING:
        print(
            f"srun: error: Unable to confirm allocation for job {job.job_id}: Job is {job.state}, not running. "
            "'squeue --me' shows what is still going.",
            file=sys.stderr,
        )
        return 1
    if node is not None and node != NODE_NAME:
        print(f"srun: error: Unable to create step for job {job.job_id}: Requested node configuration is not available", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env.update(
        _job_environment(
            job_id=job.job_id,
            name=job.name,
            user=job.user,
            cpus=job.cpus,
            mem_mb=job.mem_mb,
            gpu_ids=job.gpu_ids,
            ntasks=job.ntasks,
            workdir=job.workdir,
        )
    )
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"slurmstepd: error: execve(): {command[0]}: {exc.strerror}", file=sys.stderr)
        return 2
