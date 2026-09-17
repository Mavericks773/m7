from __future__ import annotations

import uuid
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
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
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .dungeon_catalog import dungeon_types, instances, max_batch
from .dungeon_config import fixed_dungeon_patch, merge_config_patch, power_plan_patch, weekly_patch
from .plan_editor import PlanEditor
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
        self.setObjectName("accountSettings")
        self.setStyleSheet("""
            QDialog#accountSettings, QScrollArea#settingsScroll,
            QWidget#settingsViewport, QWidget#settingsContent {background:#ffffff;}
        """)
        self.setMinimumWidth(720)
        self.resize(800, 780)
        self.account = account
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.viewport().setObjectName("settingsViewport")
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("settingsContent")
        form = QFormLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
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
        self.power = QCheckBox("完整日常中清体力")
        self.power.setChecked(account["config"].get("power_enable", True))
        self.original_build_target = bool(account["config"].get("build_target_enable", False))
        self.build_target = QCheckBox("启用培养目标（由上游自动选择副本）")
        self.build_target.setChecked(self.original_build_target)
        self._build_target_before_fixed = None
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
        dungeon = account.get("dungeon", {})
        self.dungeon_names = dict(dungeon.get("instance_names", {}))
        self.dungeon_counts = dict(dungeon.get("challenge_counts", {}))
        self.apply_fixed = QCheckBox("应用手选固定副本（会关闭覆盖目标的计划和活动）")
        self.apply_plan = QCheckBox("编辑多副本计划与兜底副本（关闭培养目标、双倍活动和优先合成）")
        self.plan_editor = PlanEditor(dungeon.get("power_plan", []))
        self.plan_keep = QCheckBox("保留整份计划：每次清体力都重复执行")
        self.plan_keep.setChecked(bool(dungeon.get("power_plan_keep", False)))
        self.apply_weekly = QCheckBox("修改独立周本设置")
        self.weekly_enabled = QCheckBox("完整日常中先打历战余响（启用将关闭培养目标）")
        self.weekly_enabled.setChecked(bool(dungeon.get("weekly_enabled", False)))
        self.weekly_day = QComboBox()
        for day, label in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日"), 1):
            self.weekly_day.addItem(label, day)
        self.weekly_day.setCurrentIndex(self.weekly_day.findData(dungeon.get("weekly_day", 1)))
        self.weekly_name = QComboBox()
        self.weekly_name.addItem("请选择周本", None)
        for key, description in instances("历战余响").items():
            self.weekly_name.addItem(f"{key} · {description}", key)
        old_weekly = dungeon.get("weekly_name")
        index = self.weekly_name.findData(old_weekly)
        if old_weekly and index < 0:
            self.weekly_name.addItem(f"{old_weekly} · 目录未收录", old_weekly)
            index = self.weekly_name.count() - 1
        self.weekly_name.setCurrentIndex(max(0, index))
        self.instance_type = QComboBox()
        self.instance_type.addItem("请选择副本类型", None)
        for instance_type in dungeon_types():
            self.instance_type.addItem(instance_type, instance_type)
        current_type = dungeon.get("instance_type")
        current_index = self.instance_type.findData(current_type)
        if current_index >= 0:
            self.instance_type.setCurrentIndex(current_index)
        self.instance_name = QComboBox()
        self.instance_name.setEditable(True)
        self.instance_name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.instance_name.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.instance_name.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.challenge_count = QSpinBox()
        self.challenge_count.setSuffix(" 次/批")
        self.instance_type.currentIndexChanged.connect(self._update_instances)
        self.apply_fixed.toggled.connect(self._fixed_toggled)
        self.apply_plan.toggled.connect(self._plan_toggled)
        self.apply_weekly.toggled.connect(self._weekly_toggled)
        self.weekly_enabled.toggled.connect(self._weekly_toggled)
        self._update_instances()
        self._set_dungeon_enabled(False)
        self.plan_editor.setEnabled(False)
        self.plan_keep.setEnabled(False)
        self._weekly_toggled()
        active = bool(account.get("run") and account["run"]["state"] in ACTIVE)
        self.apply_plan.setEnabled(not active)
        if active and dungeon.get("power_plan"):
            self.apply_fixed.setEnabled(False)
        conflict_names = {
            "build_target_enable": "培养目标",
            "power_plan": "体力计划",
            "power_plan_keep": "计划保留",
            "echo_of_war_enable": "历战余响",
            "activity_gardenofplenty_enable": "花藏繁生",
            "activity_realmofthestrange_enable": "异器盈界",
            "activity_planarfissure_enable": "位面分裂",
            "merge_immersifier": "优先合成沉浸器",
            "config_error": "配置读取异常",
        }
        conflicts = [conflict_names.get(key, key) for key in dungeon.get("conflicts", [])]
        strategy = (
            "当前为手选固定副本模式。"
            if dungeon.get("fixed_mode")
            else "当前体力计划先执行，之后使用兜底副本；周本单独设置。"
            if dungeon.get("power_plan")
            else "当前可能覆盖手选目标：" + ("、".join(conflicts) if conflicts else "未识别的设置")
        )
        self.dungeon_strategy = QLabel(strategy)
        self.dungeon_strategy.setWordWrap(True)
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
        form.addRow(QLabel("自动副本"))
        form.addRow(self.build_target)
        form.addRow(self.dungeon_strategy)
        form.addRow(self.apply_fixed)
        form.addRow(self.apply_plan)
        form.addRow(self.plan_editor)
        form.addRow(self.plan_keep)
        plan_note = QLabel(
            "计划从上到下执行；不保留时，上游扣减剩余次数。运行中不能编辑或清空计划。\n"
            "最多 20 项，每项 1～999 次；同类型只能使用同一名称，兜底也须一致。\n"
            "计划后继续刷下方兜底副本；失败不保证立即停止后续项，实际次数以游戏为准。"
        )
        plan_note.setWordWrap(True)
        form.addRow(plan_note)
        form.addRow("固定／兜底类型", self.instance_type)
        form.addRow("固定／兜底副本", self.instance_name)
        form.addRow("连续挑战", self.challenge_count)
        dungeon_note = QLabel(
            "次数是每批连续挑战数，不是任务总次数；清体力会继续使用可用资源。\n"
            "饰品提取可能消耗沉浸器。只想刷所选副本时，请运行“仅清体力”。"
        )
        dungeon_note.setWordWrap(True)
        form.addRow(dungeon_note)
        form.addRow(self.apply_weekly)
        form.addRow(self.weekly_enabled)
        form.addRow("周本目标", self.weekly_name)
        form.addRow("从每周这天开始", self.weekly_day)
        weekly_note = QLabel(
            "周本仅随完整日常且清体力开关开启时执行，在普通计划前消耗体力。\n"
            "从选定星期起至周日检查；次数和每周刷新由上游读取游戏决定。仅清体力不打周本。\n"
            "应用固定模式默认关闭周本；同时勾选修改周本设置可独立启用。"
        )
        weekly_note.setWordWrap(True)
        form.addRow(weekly_note)
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
        outer.addWidget(buttons)

    def _fixed_toggled(self, enabled):
        if enabled:
            self.apply_plan.setChecked(False)
        self._set_dungeon_enabled(self.apply_fixed.isChecked() or self.apply_plan.isChecked())

    def _plan_toggled(self, enabled):
        if enabled:
            self.apply_fixed.setChecked(False)
        self.plan_editor.setEnabled(enabled)
        self.plan_keep.setEnabled(enabled)
        self._set_dungeon_enabled(self.apply_fixed.isChecked() or enabled)

    def _weekly_toggled(self, *_):
        edit = self.apply_weekly.isChecked()
        self.weekly_enabled.setEnabled(edit)
        self.weekly_name.setEnabled(edit and self.weekly_enabled.isChecked())
        self.weekly_day.setEnabled(edit and self.weekly_enabled.isChecked())
        self._set_dungeon_enabled(self.apply_fixed.isChecked() or self.apply_plan.isChecked())

    def _set_dungeon_enabled(self, enabled):
        self.instance_type.setEnabled(enabled)
        self.instance_name.setEnabled(enabled)
        self.challenge_count.setEnabled(enabled)
        manual = enabled or (self.apply_weekly.isChecked() and self.weekly_enabled.isChecked())
        if manual:
            if self._build_target_before_fixed is None:
                self._build_target_before_fixed = self.build_target.isChecked()
            self.build_target.setChecked(False)
            self.build_target.setEnabled(False)
        else:
            self.build_target.setEnabled(True)
            if self._build_target_before_fixed is not None:
                self.build_target.setChecked(self._build_target_before_fixed)
                self._build_target_before_fixed = None

    def _update_instances(self):
        instance_type = self.instance_type.currentData()
        if not instance_type:
            return
        previous = self.dungeon_names.get(instance_type)
        self.instance_name.clear()
        self.instance_name.addItem("请选择副本", None)
        for name, description in instances(instance_type).items():
            self.instance_name.addItem(f"{name} · {description}", name)
        selected = self.instance_name.findData(previous)
        if previous and selected < 0:
            self.instance_name.addItem(f"{previous} · 当前目录未收录", previous)
            selected = self.instance_name.count() - 1
        self.instance_name.setCurrentIndex(selected if selected >= 0 else 0)
        limit = max_batch(instance_type)
        self.challenge_count.setRange(1, limit)
        count = self.dungeon_counts.get(instance_type)
        self.challenge_count.setValue(
            count if type(count) is int and 1 <= count <= limit else limit
        )

    def accept(self):
        try:
            self.payload()
        except ValueError as exc:
            QMessageBox.warning(self, "副本设置未完成", str(exc))
            return
        super().accept()

    def _selected_instance_name(self):
        index = self.instance_name.findText(
            self.instance_name.currentText(), Qt.MatchFlag.MatchExactly
        )
        return self.instance_name.itemData(index) if index >= 0 else None

    def payload(self):
        patch = {
            "daily_enable": self.daily.isChecked(),
            "power_enable": self.power.isChecked(),
            "cloud_game_use_paid_time": self.paid.isChecked(),
            "cloud_game_max_queue_time": self.queue_timeout.value(),
            "cloud_game_login_timeout": self.login_timeout.value(),
        }
        if self.build_target.isChecked() != self.original_build_target:
            patch["build_target_enable"] = self.build_target.isChecked()
        if self.apply_fixed.isChecked():
            patch.update(
                fixed_dungeon_patch(
                    self.instance_type.currentData(),
                    self._selected_instance_name(),
                    self.challenge_count.value(),
                )
            )
        if self.apply_plan.isChecked():
            patch = merge_config_patch(
                patch,
                power_plan_patch(
                    self.plan_editor.value(),
                    self.plan_keep.isChecked(),
                    self.instance_type.currentData(),
                    self._selected_instance_name(),
                    self.challenge_count.value(),
                ),
            )
            old = self.account.get("dungeon", {})
            if patch["power_plan"] == old.get("power_plan", []):
                patch.pop("power_plan")
            if patch["power_plan_keep"] == old.get("power_plan_keep", False):
                patch.pop("power_plan_keep")
        if self.apply_weekly.isChecked():
            patch = merge_config_patch(
                patch,
                weekly_patch(
                    self.weekly_enabled.isChecked(),
                    self.weekly_day.currentData(),
                    self.weekly_name.currentData(),
                ),
            )
        return {
            "account_id": self.account["id"],
            "name": self.name.text(),
            "enabled": self.enabled.isChecked(),
            "local_time": self.local_time.text(),
            "scheduled": self.scheduled.isChecked(),
            "timeout": self.timeout.value() * 60,
            "patch": patch,
            "expected_dungeon_version": (
                self.account.get("dungeon", {}).get("version")
                if self.apply_fixed.isChecked()
                or self.apply_plan.isChecked()
                or self.apply_weekly.isChecked()
                else None
            ),
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
        run_dungeon = account.get("run_dungeon")
        dungeon = (run_dungeon or {}) if active else account.get("dungeon", {})
        if dungeon.get("instance_type") and dungeon.get("instance_name"):
            prefix = (
                "本次清体力" if active else "下次清体力" if dungeon.get("pending") else "清体力"
            )
            text += f"\n{prefix}：{dungeon['instance_type']} · {dungeon['instance_name']}"
            if dungeon.get("batch_count"):
                text += f" · {dungeon['batch_count']} 次/批"
            if dungeon.get("power_plan"):
                mode = "每次保留重跑" if dungeon.get("power_plan_keep") else "扣减剩余次数"
                text += f"\n先执行 {len(dungeon['power_plan'])} 项体力计划（{mode}），再刷上述兜底"
            elif "build_target_enable" in dungeon.get("conflicts", []):
                text += "（可能被计划或培养目标覆盖）"
        elif active:
            text += "\n本次清体力配置准备中"
        if dungeon.get("weekly_enabled"):
            text += (
                f"\n周本：{dungeon.get('weekly_name')} · 周{dungeon.get('weekly_day')}起"
                "（完整日常中先于普通计划执行）"
            )
        if active and account.get("dungeon", {}).get("pending"):
            upcoming = account["dungeon"]
            text += (
                f"\n下次清体力：{upcoming.get('instance_type')} · {upcoming.get('instance_name')}"
            )
            if upcoming.get("power_plan"):
                text += f" · {len(upcoming['power_plan'])} 项计划"
            text += (
                f"\n下次周本：{upcoming.get('weekly_name')} · 周{upcoming.get('weekly_day')}起"
                if upcoming.get("weekly_enabled")
                else "\n下次周本：关闭"
            )
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
        logs_page = QWidget()
        logs_layout = QHBoxLayout(logs_page)
        logs_layout.setSpacing(12)
        self.log_panes = []
        self.log_labels = []
        for index in range(2):
            column = QVBoxLayout()
            label = QLabel(f"账号 {index + 1}")
            label.setStyleSheet("font-weight:600;color:#103b50;")
            pane = QPlainTextEdit()
            pane.setReadOnly(True)
            pane.setMaximumBlockCount(1500)
            pane.setPlaceholderText("等待日志…")
            column.addWidget(label)
            column.addWidget(pane)
            logs_layout.addLayout(column, 1)
            self.log_labels.append(label)
            self.log_panes.append(pane)
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
        for index, account in enumerate(data["accounts"]):
            self.cards[index].update_account(account)
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
        accounts = self.latest["accounts"]
        for index, (pane, label) in enumerate(zip(self.log_panes, self.log_labels)):
            account = accounts[index] if index < len(accounts) else None
            label.setText(account["display_name"] if account else f"账号 {index + 1}")
            text = account["log_tail"] if account else ""
            if pane.toPlainText() != text:
                bar = pane.verticalScrollBar()
                at_end = bar.value() >= bar.maximum() - 2
                previous = bar.value()
                pane.setPlainText(text)
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
