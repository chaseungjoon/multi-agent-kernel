"""The commit path: an ordered list of checks, then one transactional apply.

:class:`~mak.session.commit.pipeline.CommitPipeline` runs each
:class:`~mak.session.commit.verdict.CommitCheck` in order and stops at the first
verdict that is not ``accept``; :class:`~mak.session.commit.apply.CommitApplier`
then commits what every check accepted.
"""
