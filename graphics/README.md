# MAK, illustrated

### Same file, different symbols

Agents hold separate write grants; the kernel rebuilds the file from the node store.

![Three agents edit separate locked functions that rebuild into one Python file.](01-shared-file.png)

### Inspired by shared memory

Threads share protected memory. MAK applies that idea to agents and source nodes.

![Operating-system threads and shared memory mirrored by MAK agents and the node store.](02-shared-memory.png)

### Inside the kernel

The simplified path of an accepted edit, from planning to reconstructed files.

![Task flows through planner, scheduler and locks, agents, validation, node store, and file reconstruction.](03-inside-the-kernel.png)

### Worktrees versus shared nodes

Separate copies merge later; MAK coordinates access before edits.

![Worktree copies converge at a merge; MAK agents obtain locks before editing shared nodes.](04-worktrees-vs-mak.png)

### Work in waves

Independent tasks run together; a dependent task waits for both results.

![Parse and render run in the first wave; integration starts after both complete.](05-waves.png)

[Full-width preview](index.html) · [Generation prompts](prompts/)

Built with the built-in image-generation tool. Based on the repository's architecture, scheduler, lock rules, and node-store documentation. These are conceptual illustrations: validation and file reconstruction participate in a transaction; review, retries, recovery, and Git auditing are omitted from the overview.
