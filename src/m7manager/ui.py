from __future__ import annotations

import uuid
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .scheduler import ZONE
from .storage import ACTIVE

STATE_TEXT = {
    "PREPARING": "准备中",
    "STARTING": "启动中",
    "RUNNING": "运行中",
    "WAITING_LOGIN": "等待扫码",
    "RECONCILING": "正在协调状态",
    "STOPPING": "停止中",
    "EXITED": "正常退出 · 完成情况未确认",
    "FAILED": "运行失败",
    "CANCELLED": "已停止",
    "TIMED_OUT": "已超时",
    "MISSED": "已错过",
}
AUTH_TEXT = {
    "UNINITIALIZED": "尚未初始化",
    "READY": "已保存登录环境",
    "REAUTH_REQUIRED": "需要重新扫码",
}


def format_time(value):
    return datetime.fromtimestamp(value, ZONE).strftime("%m-%d %H:%M:%S") if value else "—"


def app_icon():
    pix = QPixmap(64, 64)
    pix.fill(QColor("#1b8199"))
    painter = QPainter(pix)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPixelSize(26)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), 0x84, "M7")
    painter.end()
    return QIcon(pix)


class Worker(QObject):
    updated = Signal(object)
    message = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.timer = None

    @Slot()
    def start(self):
        self.timer = QTimer(self)
        self.timer.setInterval(2500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @Slot()
    def refresh(self):
        try:
            self.manager.tick()
            self.updated.emit(self.manager.snapshot())
            if self.manager.shutting_down and not self.manager.store.active():
                self.timer.stop()
                self.manager.close()
                self.finished.emit()
        except Exception as exc:
            # Periodic errors must not open a modal dialog on every timer tick.
            self.message.emit("状态刷新异常：" + str(exc))

    @Slot(str, object)
    def command(self, action, payload):
        try:
            if action == "add":
                self.manager.add_account(payload)
            elif action == "run":
                self.manager.enqueue(payload["account"], payload["task"], uuid.uuid4().hex)
            elif action == "all":
                for account in self.manager.snapshot()["accounts"]:
                    if account["enabled"]:
                        self.manager.enqueue(account["id"])
            elif action == "stop":
                self.manager.cancel(payload)
            elif action == "edit":
                self.manager.edit_account(**payload)
            elif action == "concurrency":
                self.manager.set_concurrency(payload)
            elif action == "image":
                self.message.emit("正在下载官方镜像，首次下载可能需要数分钟…")
                digest = self.manager.prepare_image()
                self.message.emit("镜像已固定；首次任务仍需扫码并验证。" + digest[-16:])
            elif action == "export":
                self.message.emit("诊断摘要已保存：" + self.manager.export_diagnostics())
            elif action == "quit":
                self.manager.shutdown()
            self.refresh()
        except Exception as exc:
            self.failed.emit(str(exc))


class SettingsDialog(QDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("账号设置")
        self.setMinimumWidth(440)
        self.account = account
        form = QFormLayout(self)
        self.name = QLineEdit(account["display_name"])
        self.enabled = QCheckBox("启用此账号")
        self.enabled.setChecked(bool(account["enabled"]))
        self.scheduled = QCheckBox("每日定时执行完整日常")
        self.scheduled.setChecked(bool(account["schedule"]["enabled"]))
        self.local_time = QLineEdit(account["schedule"]["local_time"])
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 240)
        self.timeout.setSuffix(" 分钟")
        self.timeout.setValue(account["timeout_seconds"] // 60)
        self.daily = QCheckBox("每日实训")
        self.daily.setChecked(account["config"].get("daily_enable", True))
        self.power = QCheckBox("清体力")
        self.power.setChecked(account["config"].get("power_enable", True))
        self.paid = QCheckBox("允许使用付费时长（不执行充值）")
        self.paid.setChecked(account["config"].get("cloud_game_use_paid_time", False))
        self.queue_timeout = QSpinBox()
        self.queue_timeout.setRange(1, 120)
        self.queue_timeout.setSuffix(" 分钟")
        self.queue_timeout.setValue(account["config"].get("cloud_game_max_queue_time", 15))
        self.login_timeout = QSpinBox()
        self.login_timeout.setRange(1, 30)
        self.login_timeout.setSuffix(" 分钟")
        self.login_timeout.setValue(account["config"].get("cloud_game_login_timeout", 10))
        form.addRow("名称", self.name)
        form.addRow(self.enabled)
        form.addRow(self.scheduled)
        form.addRow("北京时间 HH:mm", self.local_time)
        form.addRow("任务总超时", self.timeout)
        form.addRow(self.daily)
        form.addRow(self.power)
        form.addRow(self.paid)
        form.addRow("云游戏排队上限", self.queue_timeout)
        form.addRow("扫码登录超时", self.login_timeout)
        note = QLabel(
            "任务设置会在下次运行前应用。\n停用账号会取消排队任务；当前任务请使用“停止”。"
        )
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def payload(self):
        return {
            "account_id": self.account["id"],
            "name": self.name.text(),
            "enabled": self.enabled.isChecked(),
            "local_time": self.local_time.text(),
            "scheduled": self.scheduled.isChecked(),
            "timeout": self.timeout.value() * 60,
            "patch": {
                "daily_enable": self.daily.isChecked(),
                "power_enable": self.power.isChecked(),
                "cloud_game_use_paid_time": self.paid.isChecked(),
                "cloud_game_max_queue_time": self.queue_timeout.value(),
                "cloud_game_login_timeout": self.login_timeout.value(),
            },
        }


class AccountCard(QFrame):
    request = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.account = None
        self.setObjectName("accountCard")
        layout = QVBoxLayout(self)
        self.title = QLabel("空账号槽位")
        self.title.setObjectName("cardTitle")
        self.status = QLabel("添加账号后，分别扫码保存登录环境。")
        self.status.setWordWrap(True)
        self.next = QLabel("每天的日常，在这里安排。")
        self.next.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.next)
        self.task = QComboBox()
        for name, task in (
            ("完整日常", "main"),
            ("每日实训子任务", "daily"),
            ("仅清体力", "power"),
        ):
            self.task.addItem(name, task)
        layout.addWidget(self.task)
        row = QHBoxLayout()
        self.run_button = QPushButton("初始化并运行")
        self.run_button.setObjectName("primary")
        self.stop_button = QPushButton("停止")
        self.settings_button = QPushButton("设置")
        row.addWidget(self.run_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.settings_button)
        layout.addLayout(row)
        self.qr = QLabel("等待任务生成登录二维码")
        self.qr.setMinimumHeight(260)
        self.qr.setStyleSheet("background:#edf2f5;border-radius:12px;color:#314a5c;padding:10px;")
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setWordWrap(True)
        layout.addWidget(self.qr)
        self.hint = QLabel("扫码成功后将继续执行所选任务。\n登录失效时需要再次扫码。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.run_button.clicked.connect(self.run)
        self.stop_button.clicked.connect(
            lambda: self.request.emit("stop", self.account["id"]) if self.account else None
        )
        self.settings_button.clicked.connect(self.edit)
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.settings_button.setEnabled(False)

    def run(self):
        if self.account:
            self.request.emit(
                "run", {"account": self.account["id"], "task": self.task.currentData()}
            )

    def edit(self):
        dialog = SettingsDialog(self.account, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.request.emit("edit", dialog.payload())

    def update_account(self, account):
        self.account = account
        self.title.setText(account["display_name"])
        run = account["run"]
        active = bool(run and run["state"] in ACTIVE)
        label = (
            STATE_TEXT.get(run["state"], run["state"])
            if run
            else AUTH_TEXT.get(account["auth_state"], "待运行")
        )
        if account["queued"]:
            label += " · 已排队"
        if not account["enabled"]:
            label += " · 已停用"
        if account["blocked_reason"]:
            label += "\n自动任务已暂停：" + account["blocked_reason"]
        self.status.setText(label)
        schedule = account["schedule"]
        text = (
            "下次计划：" + format_time(schedule["next_run_at"]) + " 北京时间"
            if schedule["enabled"]
            else "定时未启用"
        )
        if run and active and run["deadline_at"]:
            text += "\n最晚结束：" + format_time(run["deadline_at"])
        self.next.setText(text)
        self.run_button.setText("立即运行" if account["auth_state"] == "READY" else "初始化并运行")
        self.run_button.setEnabled(
            bool(account["enabled"]) and not active and not account["queued"]
        )
        self.stop_button.setEnabled(active or bool(account["queued"]))
        self.settings_button.setEnabled(True)
        pix = QPixmap()
        if account["qr_bytes"] and pix.loadFromData(account["qr_bytes"]):
            self.qr.setPixmap(
                pix.scaled(
                    240,
                    240,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.qr.clear()
            self.qr.setText(
                "正在等待本次登录二维码…" if active else "运行时如需登录，二维码会显示在这里"
            )


class MainWindow(QMainWindow):
    command = Signal(str, object)

    def __init__(self, manager, start_worker=True):
        super().__init__()
        self.setWindowTitle("M7 · 双账号管理器")
        self.setWindowIcon(app_icon())
        self.resize(1100, 850)
        self.latest = None
        self.quitting = False
        self.thread = None
        self.notified = {}
        self.statusBar().showMessage("本地运行 · 默认串行 · 游戏完成情况须以实际结果核对")
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(26, 22, 26, 18)
        title = QLabel("M7  /  双账号工作台")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("独立登录 · 定时日常 · 运行记录")
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.add = QPushButton("＋ 添加账号")
        self.image_button = QPushButton("准备官方镜像")
        self.run_all = QPushButton("运行全部（按并发设置）")
        self.parallel = QComboBox()
        self.parallel.addItem("串行 · 1 个任务", 1)
        self.parallel.addItem("并行 · 2 个任务", 2)
        for widget in (self.add, self.image_button, self.run_all, self.parallel):
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        self.connection = QLabel("正在检查本地 Docker…")
        self.connection.setWordWrap(True)
        layout.addWidget(self.connection)
        self.alert = QLabel("")
        self.alert.setWordWrap(True)
        self.alert.setStyleSheet("color:#7a3f00;font-weight:600;")
        layout.addWidget(self.alert)
        card_row = QHBoxLayout()
        self.cards = [AccountCard(), AccountCard()]
        for card in self.cards:
            card.request.connect(self.command.emit)
            card_row.addWidget(card)
        layout.addLayout(card_row)
        self.tabs = QTabWidget()
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)
        self.log_account = QComboBox()
        logs_page = QWidget()
        logs_layout = QVBoxLayout(logs_page)
        logs_layout.addWidget(self.log_account)
        logs_layout.addWidget(self.logs)
        self.tabs.addTab(logs_page, "最近日志（保留尾部）")
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(
            ["账号", "开始时间", "任务", "进程结果", "游戏完成情况"]
        )
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.history, "运行记录")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.setStyleSheet("""
            QMainWindow, QDialog {background:#edf3f7;color:#172b3a;}
            QWidget {
                color:#172b3a;
                font-family:'Microsoft YaHei UI';
                font-size:13px;
            }
            QLabel {color:#263f50;}
            QLabel#title {font-size:27px;font-weight:700;color:#0f354b;}
            QLabel#subtitle {color:#40596b;padding-bottom:10px;}
            QFrame#accountCard {
                background:#ffffff;
                border:1px solid #cbd9e2;
                border-radius:14px;
            }
            QLabel#cardTitle {font-size:20px;font-weight:600;color:#103b50;}
            QPushButton {
                color:#163548;
                background:#ffffff;
                border:1px solid #b9ccd8;
                border-radius:7px;
                padding:8px 12px;
            }
            QPushButton:hover {color:#0d3042;background:#dcecf2;border-color:#6f9fb2;}
            QPushButton:pressed {background:#c9e0e8;}
            QPushButton:disabled {color:#667985;background:#e2e9ed;border-color:#d1dce2;}
            QPushButton#primary {background:#126f87;color:#ffffff;border:0;}
            QPushButton#primary:hover {background:#0e6076;color:#ffffff;}
            QPushButton#primary:disabled {background:#aec2ca;color:#536976;}
            QComboBox, QLineEdit, QSpinBox {
                color:#142c3a;
                background:#ffffff;
                border:1px solid #b9ccd8;
                border-radius:5px;
                padding:6px;
                selection-background-color:#126f87;
                selection-color:#ffffff;
            }
            QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled {
                color:#667985;
                background:#e7edf0;
            }
            QComboBox QAbstractItemView {
                color:#142c3a;
                background:#ffffff;
                border:1px solid #9fb7c5;
                selection-background-color:#126f87;
                selection-color:#ffffff;
            }
            QCheckBox {color:#203b4c;spacing:7px;}
            QPlainTextEdit, QTableWidget {
                color:#172b3a;
                background:#ffffff;
                border:1px solid #c4d4de;
                selection-background-color:#126f87;
                selection-color:#ffffff;
            }
            QHeaderView::section {
                color:#17394b;
                background:#dce8ee;
                border:0;
                border-right:1px solid #c2d2dc;
                border-bottom:1px solid #b7cbd7;
                padding:7px;
                font-weight:600;
            }
            QTabWidget::pane {background:#ffffff;border:1px solid #c4d4de;}
            QTabBar::tab {
                color:#294757;
                background:#dce6ec;
                border:1px solid #bccdd7;
                padding:7px 14px;
            }
            QTabBar::tab:selected {color:#10384c;background:#ffffff;font-weight:600;}
            QStatusBar {color:#294757;background:#e1eaf0;}
            QMenu {color:#172b3a;background:#ffffff;border:1px solid #b9ccd8;}
            QMenu::item:selected {color:#ffffff;background:#126f87;}
            QToolTip {color:#ffffff;background:#203b4c;border:1px solid #486777;}
        """)
        self.add.clicked.connect(self.add_account)
        self.image_button.clicked.connect(self.prepare_image)
        self.run_all.clicked.connect(lambda: self.command.emit("all", None))
        self.parallel.currentIndexChanged.connect(
            lambda: self.command.emit("concurrency", self.parallel.currentData())
        )
        self.log_account.currentIndexChanged.connect(self.update_log)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu(self)
        show = QAction("显示工作台", self)
        show.triggered.connect(self.showNormal)
        export = QAction("导出诊断摘要", self)
        export.triggered.connect(lambda: self.command.emit("export", None))
        quit_action = QAction("停止任务并退出", self)
        quit_action.triggered.connect(self.request_quit)
        menu.addAction(show)
        menu.addAction(export)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.showNormal()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        if start_worker:
            self.thread = QThread(self)
            self.worker = Worker(manager)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.start)
            self.command.connect(self.worker.command)
            self.worker.updated.connect(self.update_snapshot)
            self.worker.message.connect(lambda text: self.statusBar().showMessage(text))
            self.worker.failed.connect(self.show_error)
            self.worker.finished.connect(self.thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(QApplication.instance().quit)
            self.thread.start()

    def add_account(self):
        name, accepted = QInputDialog.getText(self, "添加账号", "账号名称（用于区分登录环境）")
        if accepted:
            self.command.emit("add", name)

    def prepare_image(self):
        self.image_button.setEnabled(False)
        self.command.emit("image", None)

    def show_error(self, text):
        self.statusBar().showMessage(text)
        if self.latest:
            self.image_button.setEnabled(self.latest["active_count"] == 0 and not self.quitting)
        QMessageBox.warning(self, "操作未完成", text)

    def update_snapshot(self, data):
        first = self.latest is None
        self.latest = data
        self.connection.setText(data["docker_status"] + f" · 已错过计划 {data['missed']} 次")
        self.alert.setText("\n".join(data["alerts"]))
        self.add.setEnabled(len(data["accounts"]) < 2 and not self.quitting)
        self.image_button.setEnabled(data["active_count"] == 0 and not self.quitting)
        self.image_button.setText("检查固定镜像" if data["image_digest"] else "准备官方镜像")
        self.image_button.setToolTip(data["image_digest"] or "首次下载后固定版本，后续不会自动升级")
        self.parallel.blockSignals(True)
        self.parallel.setCurrentIndex(data["concurrency"] - 1)
        self.parallel.blockSignals(False)
        current_id = self.log_account.currentData()
        self.log_account.blockSignals(True)
        self.log_account.clear()
        for index, account in enumerate(data["accounts"]):
            self.cards[index].update_account(account)
            self.log_account.addItem(account["display_name"], account["id"])
            run = account["run"]
            if run:
                state = run["state"]
                previous = self.notified.get(run["id"])
                if (
                    not first
                    and previous != state
                    and state in ("EXITED", "FAILED", "TIMED_OUT", "WAITING_LOGIN")
                ):
                    self.tray.showMessage(account["display_name"], STATE_TEXT[state])
                self.notified[run["id"]] = state
        idx = self.log_account.findData(current_id)
        if idx >= 0:
            self.log_account.setCurrentIndex(idx)
        self.log_account.blockSignals(False)
        self.update_log()
        self.history.setRowCount(len(data["history"]))
        for row, run in enumerate(data["history"]):
            values = [
                run["display_name"],
                format_time(run["started_at"] or run["created_at"]),
                run["task"],
                STATE_TEXT.get(run["state"], run["state"]),
                "未确认",
            ]
            for column, value in enumerate(values):
                self.history.setItem(row, column, QTableWidgetItem(value))

    def update_log(self):
        if not self.latest:
            return
        account = next(
            (a for a in self.latest["accounts"] if a["id"] == self.log_account.currentData()), None
        )
        text = account["log_tail"] if account else ""
        if self.logs.toPlainText() != text:
            bar = self.logs.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 2
            previous = bar.value()
            self.logs.setPlainText(text)
            bar.setValue(bar.maximum() if at_end else previous)

    def request_quit(self):
        if not self.thread:
            self.quitting = True
            self.close()
            return
        self.quitting = True
        self.statusBar().showMessage(
            "正在停止任务并退出；如 Docker 中断，将等待连接恢复以确认任务已停止。"
        )
        self.command.emit("quit", None)

    def closeEvent(self, event):
        if self.quitting and not self.thread:
            event.accept()
        elif self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "M7 仍在运行", "窗口已隐藏到托盘，定时任务继续运行。退出请使用托盘菜单。"
            )
        else:
            event.ignore()
            self.request_quit()
