

# ---------------------------------------------------------------------------
# Added by training-environment-app-creator (docker/scripts/slurm-cpu-node.py)
#
# The scheduler above is written for a GPU node. This app has no emulated GPUs,
# so these overrides make the same scheduler present a plain CPU node: its name
# and partition come from the environment, and SLURM_EMU_GPUS=0 gives the node
# no GPUs at all, so a job that asks for one is refused at submission, as it
# would be on a CPU-only partition.
#
# They are module-level names, looked up when each command runs, so replacing
# them at the end of the module changes every command that uses them.
# ---------------------------------------------------------------------------
import os as _app_creator_os

NODE_NAME = _app_creator_os.environ.get("SLURM_EMU_NODE_NAME", NODE_NAME)
PARTITION = _app_creator_os.environ.get("SLURM_EMU_PARTITION", PARTITION)

if _app_creator_os.environ.get("SLURM_EMU_GPUS", "").strip() == "0":

    def node_gres() -> list[str]:
        """No GPUs on this node."""
        return []
