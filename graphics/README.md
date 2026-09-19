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

### Simulated scaling results

Four-agent makespan across uniform and concentrated contention in the keyless
Wave 21 smoke sweep.

![MAK and worktree makespan under uniform and Zipf contention.](06-simulated-scaling-results.png)

[Full-width preview](index.html) · [Generation prompts](prompts/)

The conceptual illustrations were built with the image-generation tool and are
based on the repository's architecture, scheduler, lock rules, and node-store
documentation. The scaling chart is rendered from the Wave 21 smoke results.
