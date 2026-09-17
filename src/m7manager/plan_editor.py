from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from .dungeon_catalog import dungeon_types, instances
from .dungeon_config import PLAN_COUNT_LIMIT, PLAN_LIMIT


class PlanEditor(QWidget):
    """Edits ordered targets without touching the running account configuration."""

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["类型", "副本", "计划次数"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("新增")
        self.remove_button = QPushButton("删除选中项")
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.add_button.clicked.connect(lambda: self.add_row())
        self.remove_button.clicked.connect(self.remove_row)
        self.up_button.clicked.connect(lambda: self.move_row(-1))
        self.down_button.clicked.connect(lambda: self.move_row(1))
        for item in plan:
            self.add_row(item)

    def add_row(self, item=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        kind = QComboBox()
        kind.addItem("请选择类型", None)
        for value in dungeon_types():
            kind.addItem(value, value)
        name = QComboBox()
        count = QSpinBox()
        count.setRange(1, PLAN_COUNT_LIMIT)
        for column, widget in enumerate((kind, name, count)):
            self.table.setCellWidget(row, column, widget)

        def populate():
            name.clear()
            name.addItem("请选择副本", None)
            for key, description in instances(kind.currentData()).items():
                if key != "无":
                    name.addItem(f"{key} · {description}", key)

        kind.currentIndexChanged.connect(populate)
        if item and isinstance(item, list) and len(item) == 3:
            for combo, value in ((kind, item[0]),):
                index = combo.findData(value)
                if index < 0:
                    combo.addItem(f"{value} · 目录未收录", value)
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
            populate()
            index = name.findData(item[1])
            if index < 0:
                name.addItem(f"{item[1]} · 目录未收录", item[1])
                index = name.count() - 1
            name.setCurrentIndex(index)
            if type(item[2]) is int:
                count.setRange(min(1, item[2]), max(PLAN_COUNT_LIMIT, item[2]))
                count.setValue(item[2])
        else:
            populate()
        self.table.selectRow(row)
        self.add_button.setEnabled(self.table.rowCount() < PLAN_LIMIT)

    def value(self):
        return [
            [
                self.table.cellWidget(row, 0).currentData(),
                self.table.cellWidget(row, 1).currentData(),
                self.table.cellWidget(row, 2).value(),
            ]
            for row in range(self.table.rowCount())
        ]

    def remove_row(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
        self.add_button.setEnabled(self.table.rowCount() < PLAN_LIMIT)

    def move_row(self, offset):
        row = self.table.currentRow()
        target = row + offset
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        plan = self.value()
        plan[row], plan[target] = plan[target], plan[row]
        self.table.setRowCount(0)
        for item in plan:
            self.add_row(item)
        self.table.selectRow(target)
