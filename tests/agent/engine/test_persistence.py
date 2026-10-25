"""A failed state replacement must not corrupt the previous jobs or memories."""

from functools import partial
from pathlib import Path

import pytest

from physiclaw.agent.engine import job_store, jobs, memory, pitfalls


@pytest.mark.parametrize("operation", ["finish_job", "purge_job", "memory", "pitfalls"])
@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_state_replacement_preserves_previous_bytes(
    isolated_job_store, mem_paths, mocker, operation, failure
):
    if operation == "memory":
        memory.save_fact("keep this fact")
        path = memory.MEMORY_FILE
        update = partial(memory.update_fact, "keep this fact", "updated fact")
    elif operation == "pitfalls":
        pitfalls.add(["keep this lesson"])
        path = pitfalls.paths.pitfalls_dir() / "pitfalls.md"
        update = partial(pitfalls.replace, ["updated lesson"])
    else:
        jobs.create_job(
            id="existing",
            description="original",
            schedule="0 9 * * *",
            context="enough context for the task",
        )
        path = isolated_job_store
        if operation == "finish_job":
            update = partial(
                jobs.finish_job, id="existing", status="done", recap="finished"
            )
        else:
            update = partial(job_store._remove_sections, path, {"existing"})
    before = path.read_bytes()
    if failure == "write":
        write_bytes = Path.write_bytes

        def partial_write(target, content):
            write_bytes(target, content[:3])
            raise OSError("disk full")

        mocker.patch.object(Path, "write_bytes", partial_write)
    else:
        mocker.patch.object(Path, "replace", side_effect=OSError("cannot replace"))

    with pytest.raises(OSError):
        update()

    assert path.read_bytes() == before
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
