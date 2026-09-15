"""Background work.

Probing, spectral analysis and conversion all shell out to ffmpeg and take seconds,
so none of them may run on the GUI thread. Each is a QRunnable on the shared thread
pool, reporting back through signals.
"""

from __future__ import annotations

import logging
import traceback

from PySide6.QtCore import QObject, QRunnable, Signal

from .. import config
from ..models import QualityReport, SourceInfo, Track
from ..pipeline import convert as convert_mod
from ..pipeline import filing, probe, quality, resource_list

logger = logging.getLogger(__name__)


class AnalysisSignals(QObject):
    finished = Signal(object, object, object)   # Track, SourceInfo, QualityReport
    failed = Signal(object, str)                # Track, message


class AnalysisTask(QRunnable):
    """Probe a file, measure its quality, and render its spectrogram."""

    def __init__(self, track: Track, settings: config.Settings) -> None:
        super().__init__()
        self.track = track
        self.settings = settings
        self.signals = AnalysisSignals()

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            info: SourceInfo = probe.probe(self.track.source_path, self.settings)
            report: QualityReport = quality.analyse(info, self.settings)

            # Best effort: a missing spectrogram shouldn't fail the whole analysis.
            try:
                cached = quality.spectrogram_cache_path(self.track.source_path)
                if not cached.is_file():
                    quality.render_spectrogram(self.track.source_path, cached, self.settings)
                report.spectrogram_path = cached
            except Exception:  # noqa: BLE001
                logger.warning("Spectrogram failed for %s", self.track.source_path.name,
                               exc_info=True)

            self.signals.finished.emit(self.track, info, report)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            logger.error("Analysis failed for %s", self.track.source_path, exc_info=True)
            self.signals.failed.emit(self.track, f"{type(exc).__name__}: {exc}")
            logger.debug(traceback.format_exc())


class FilingSignals(QObject):
    finished = Signal(object, object)   # Track, final Path
    failed = Signal(object, str)


class QuarantineTask(QRunnable):
    """Move a flagged upscale out of the library and onto the re-source list."""

    def __init__(
        self, track: Track, library: filing.MusicLibrary, settings: config.Settings
    ) -> None:
        super().__init__()
        self.track = track
        self.library = library
        self.settings = settings
        self.signals = FilingSignals()

    def run(self) -> None:  # noqa: D102
        try:
            final = filing.quarantine_track(
                self.track.source_path, self.library, self.settings.quarantine_folder
            )
            if self.track.info and self.track.quality:
                resource_list.append(
                    resource_list.ReSourceEntry.build(
                        self.track.info,
                        self.track.quality,
                        resource_list.Action.QUARANTINED,
                        final,
                    )
                )
            self.signals.finished.emit(self.track, final)
        except Exception as exc:  # noqa: BLE001
            logger.error("Quarantine failed for %s", self.track.source_path, exc_info=True)
            self.signals.failed.emit(self.track, f"{type(exc).__name__}: {exc}")


class FilingTask(QRunnable):
    """Convert a track to its chosen format and move it into the library."""

    def __init__(
        self,
        track: Track,
        plan: convert_mod.ConversionPlan,
        library: filing.MusicLibrary,
        settings: config.Settings,
    ) -> None:
        super().__init__()
        self.track = track
        self.plan = plan
        self.library = library
        self.settings = settings
        self.signals = FilingSignals()

    def run(self) -> None:  # noqa: D102
        try:
            assert self.track.genre_folder, "a genre folder must be chosen before filing"

            source = self.track.source_path
            working = source

            if not self.plan.is_noop:
                # Convert alongside the source first, then move — so a failed encode
                # never leaves anything in the music library.
                converted = source.with_name(source.stem + self.plan.extension)
                if converted == source:
                    converted = source.with_name(source.stem + ".converted" + self.plan.extension)
                working = convert_mod.convert(
                    self.plan, converted, self.settings, overwrite=True
                )

            final = filing.file_track(
                working,
                self.library,
                self.track.genre_folder,
                self.track.subgenre_folder,
                collision=filing.CollisionPolicy.RENAME,
            )

            # The original is only removed once the converted file is safely filed.
            if working != source and source.exists():
                source.unlink()
                logger.info("Removed source after conversion: %s", source)

            # Filing a flagged track anyway still records it — the decision was
            # "no better copy exists", not "this is fine".
            if self.track.quality and self.track.quality.is_suspect and self.track.info:
                if resource_list.LOW_QUALITY_FLAG not in self.track.flags:
                    self.track.flags.append(resource_list.LOW_QUALITY_FLAG)
                resource_list.append(
                    resource_list.ReSourceEntry.build(
                        self.track.info,
                        self.track.quality,
                        resource_list.Action.FILED_ANYWAY,
                        final,
                    )
                )

            self.signals.finished.emit(self.track, final)
        except Exception as exc:  # noqa: BLE001
            logger.error("Filing failed for %s", self.track.source_path, exc_info=True)
            self.signals.failed.emit(self.track, f"{type(exc).__name__}: {exc}")
