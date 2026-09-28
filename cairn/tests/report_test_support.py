from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from cairn.dispatcher.tasks.report_finish_outbox import ReportFinishOutbox


class EphemeralReportFinishOutbox(ReportFinishOutbox):
    """为不验证磁盘布局的报告测试提供独立临时 journal。"""

    def __init__(self) -> None:
        self._temporary_directory = TemporaryDirectory(
            prefix="cairn-report-finish-outbox-test-"
        )
        super().__init__(Path(self._temporary_directory.name) / "journal")


def ephemeral_report_finish_outbox() -> ReportFinishOutbox:
    return EphemeralReportFinishOutbox()
