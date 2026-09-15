"""Library settings available without loading a track."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..pipeline.filing import MusicLibrary
from ..rekordbox.colours import Colour
from ..rekordbox.mytags import Category
from .general_settings import GeneralSettingsPage
from .playlist_manager import PlaylistManager
from .steps import ColourStep, FolderStep, TagsStep


class SettingsPage(QWidget):
    """Manage KRATR's library structure outside the track import wizard."""

    close_requested = Signal()
    taxonomy_edit = Signal(str, dict, str)
    library_edit = Signal(str, dict, str)
    preferences_saved = Signal()
    apply_requested = Signal()

    SECTIONS = (
        ("GENERAL", "Paths, import defaults, tagging behaviour and safety"),
        ("FOLDERS", "Organise the genre tree and reroute collection paths"),
        ("PLAYLISTS", "Manage the rekordbox playlist tree without losing memberships"),
        ("TAGS", "Manage rekordbox MyTag categories and labels"),
        ("COLOURS", "Give the eight rekordbox colours useful names"),
    )

    def __init__(
        self,
        library: MusicLibrary,
        settings: config.Settings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")

        self.general_page = GeneralSettingsPage(settings or config.Settings())
        self.folder_page = FolderStep(library, management_mode=True)
        self.playlists_page = PlaylistManager()
        self.tags_page = TagsStep(management_mode=True)
        self.colours_page = ColourStep(management_mode=True)
        self.pages = (
            self.general_page,
            self.folder_page,
            self.playlists_page,
            self.tags_page,
            self.colours_page,
        )

        self.general_page.saved.connect(self.preferences_saved)
        self.folder_page.edit_requested.connect(self.library_edit)
        self.playlists_page.edit_requested.connect(self.library_edit)
        self.tags_page.edit_requested.connect(self.taxonomy_edit)
        self.colours_page.edit_requested.connect(self.taxonomy_edit)

        self._build()
        self.show_section(0)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(1)
        eyebrow = QLabel("KRATR / LIBRARY CONTROL")
        eyebrow.setObjectName("eyebrow")
        titles.addWidget(eyebrow)
        title = QLabel("SETTINGS")
        title.setObjectName("pageTitle")
        titles.addWidget(title)
        header.addLayout(titles)
        header.addStretch(1)

        self.close_button = QPushButton("←  Back to intake")
        self.close_button.clicked.connect(self.close_requested)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(18)

        navigation = QWidget()
        navigation.setObjectName("settingsNavigation")
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(7)

        nav_label = QLabel("LIBRARY")
        nav_label.setObjectName("panelTitle")
        nav_layout.addWidget(nav_label)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (name, _description) in enumerate(self.SECTIONS):
            button = QPushButton(f"0{index + 1} / {name}")
            button.setObjectName("settingsNavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, i=index: self.show_section(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            nav_layout.addWidget(button)
        nav_layout.addStretch(1)
        body.addWidget(navigation)

        content = QVBoxLayout()
        content.setSpacing(10)
        self.section_title = QLabel()
        self.section_title.setObjectName("categoryTitle")
        content.addWidget(self.section_title)
        self.section_description = QLabel()
        self.section_description.setObjectName("muted")
        content.addWidget(self.section_description)

        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(page)
        content.addWidget(self.stack, stretch=1)
        body.addLayout(content, stretch=1)
        layout.addLayout(body, stretch=1)

        write_rail = QWidget()
        write_rail.setObjectName("actionRail")
        write_layout = QHBoxLayout(write_rail)
        write_layout.setContentsMargins(10, 8, 10, 8)
        self.pending_label = QLabel("No rekordbox changes waiting")
        self.pending_label.setObjectName("muted")
        self.pending_label.setWordWrap(True)
        write_layout.addWidget(self.pending_label, stretch=1)
        self.apply_button = QPushButton("WRITE CHANGES TO REKORDBOX")
        self.apply_button.setObjectName("primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_requested)
        write_layout.addWidget(self.apply_button)
        layout.addWidget(write_rail)

    def show_section(self, index: int) -> None:
        if not 0 <= index < len(self.pages):
            return
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        name, description = self.SECTIONS[index]
        self.section_title.setText(name)
        self.section_description.setText(description)

    def set_categories(self, categories: list[Category]) -> None:
        self.tags_page.set_categories(categories)

    def set_colours(self, colours: list[Colour]) -> None:
        self.colours_page.set_colours(colours)

    def set_playlist_tree(self, roots: list, *, clear_staged: bool = False) -> None:
        self.playlists_page.set_tree(roots, clear_staged=clear_staged)

    def set_pending(
        self,
        descriptions: list[str],
        tag_descriptions: list[str] | None = None,
        *,
        folder_edit_pending: bool = False,
    ) -> None:
        self.tags_page.set_pending(tag_descriptions if tag_descriptions is not None else descriptions)
        self.general_page.set_music_root_locked(folder_edit_pending)
        count = len(descriptions)
        self.apply_button.setEnabled(bool(count))
        if count:
            shown = "; ".join(descriptions[-4:])
            more = f" · {count - 4} more" if count > 4 else ""
            self.pending_label.setText(
                f"{count} rekordbox change(s) waiting · {shown}{more}"
            )
        else:
            self.pending_label.setText("No rekordbox changes waiting")

    def refresh_library_views(self, *, clear_staged: bool = False) -> None:
        self.folder_page.refresh_management(clear_staged=clear_staged)
        self.general_page.refresh_status()
