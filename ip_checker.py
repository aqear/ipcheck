# -*- coding: utf-8 -*-
"""
IP批量查询桌面程序
- 基础URL + 每行一个后缀（IP/域名/路径）拼接后 GET 请求
- 并发控制、进度显示、实时表格、CSV导出
- 无浏览器CORS限制
"""

import sys
import csv
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from PySide6.QtCore import Qt, QObject, Signal, QThread
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QSpinBox, QFileDialog, QMessageBox,
    QHeaderView, QProgressBar, QGroupBox, QSplitter, QCheckBox
)


# ---------------- 工作线程 ----------------
class CheckWorker(QObject):
    """后台并发查询线程"""
    progress = Signal(int, int)                 # 已完成, 总数
    row_done = Signal(int, dict)                # 行索引, 结果dict
    finished_all = Signal(list)                 # 全部结果
    status = Signal(str)                        # 状态文字

    def __init__(self, base_url, suffixes, concurrency, timeout, verify_ssl):
        super().__init__()
        self.base_url = base_url
        self.suffixes = suffixes
        self.concurrency = max(1, min(concurrency, 50))
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def join_url(base, suffix):
        """拼接URL, 处理多余斜杠"""
        b = (base or "").strip()
        s = (suffix or "").strip()
        if not b:
            return s
        if not s:
            return b
        b = b.rstrip("/")
        s = s.lstrip("/")
        return f"{b}/{s}"

    def _check_one(self, idx, suffix):
        full_url = self.join_url(self.base_url, suffix)
        start = time.perf_counter()
        try:
            r = requests.get(
                full_url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                headers={"Accept": "*/*", "User-Agent": "IPChecker/1.0"},
                allow_redirects=True,
            )
            elapsed = (time.perf_counter() - start) * 1000
            text = r.text
            truncated = text if len(text) <= 4000 else text[:4000] + "…[截断]"
            return {
                "index": idx,
                "suffix": suffix,
                "url": full_url,
                "status": r.status_code,
                "elapsed": f"{elapsed:.0f}ms",
                "body": truncated,
                "error": False,
            }
        except requests.exceptions.SSLError as e:
            return {"index": idx, "suffix": suffix, "url": full_url,
                    "status": "SSL错误", "elapsed": "-", "body": str(e), "error": True}
        except requests.exceptions.Timeout:
            return {"index": idx, "suffix": suffix, "url": full_url,
                    "status": "超时", "elapsed": "-", "body": f"请求超过 {self.timeout}s", "error": True}
        except requests.exceptions.ConnectionError as e:
            return {"index": idx, "suffix": suffix, "url": full_url,
                    "status": "连接失败", "elapsed": "-", "body": str(e), "error": True}
        except Exception as e:
            return {"index": idx, "suffix": suffix, "url": full_url,
                    "status": "请求失败", "elapsed": "-", "body": str(e), "error": True}

    def run(self):
        total = len(self.suffixes)
        if total == 0:
            self.finished_all.emit([])
            return

        results = [None] * total
        done = 0
        self.status.emit(f"开始查询，共 {total} 条，并发 {self.concurrency}…")

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(self._check_one, i, s): i
                for i, s in enumerate(self.suffixes)
            }
            for fut in as_completed(futures):
                if self._stop:
                    break
                res = fut.result()
                idx = res["index"]
                results[idx] = res
                done += 1
                self.row_done.emit(idx, res)
                self.progress.emit(done, total)

        final = [r for r in results if r is not None]
        self.finished_all.emit(final)


# ---------------- 主窗口 ----------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IP 批量查询工具 · 桌面版")
        self.resize(1280, 780)

        self.worker = None
        self.worker_thread = None
        self.results = []           # 最终结果
        self.current_suffixes = []

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ==== 顶部标题 ====
        title = QLabel("🌐 IP 批量查询 · URL 后缀拼接")
        title.setStyleSheet("font-size: 20px; font-weight: 600; color: #0b1b33;")
        root.addWidget(title)

        sub = QLabel("桌面程序 · 无CORS限制 · 并发请求 · 表格展示 · CSV导出")
        sub.setStyleSheet("color:#4a5c6f; padding:6px 10px; background:#f0f7ff;"
                          "border-left:4px solid #3b82f6; border-radius:0 8px 8px 0;")
        root.addWidget(sub)

        # ==== 配置区 ====
        cfg = QGroupBox("配置")
        cfg.setStyleSheet("QGroupBox{font-weight:600; color:#1e2f44; border:1px solid #e2e9f2;"
                          "border-radius:10px; margin-top:8px; padding-top:14px;}"
                          "QGroupBox::title{subcontrol-origin: margin; left:12px; padding:0 6px;}")
        cfg_layout = QVBoxLayout(cfg)

        # 基础URL
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("基础URL:"))
        self.base_url_edit = QLineEdit("http://localhost:8080/api/ip/")
        self.base_url_edit.setPlaceholderText("例如 http://localhost:8080/api/ip/")
        self.base_url_edit.setStyleSheet("padding:6px 10px; border:1.5px solid #dbe2eb;"
                                         "border-radius:8px; font-family: Consolas, monospace;")
        url_row.addWidget(self.base_url_edit, 1)
        cfg_layout.addLayout(url_row)

        # 并发、超时、SSL、按钮
        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("并发:"))
        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 50)
        self.spin_concurrency.setValue(10)
        self.spin_concurrency.setFixedWidth(70)
        opt_row.addWidget(self.spin_concurrency)

        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("超时(秒):"))
        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(1, 120)
        self.spin_timeout.setValue(10)
        self.spin_timeout.setFixedWidth(70)
        opt_row.addWidget(self.spin_timeout)

        opt_row.addSpacing(12)
        self.chk_ssl = QCheckBox("校验SSL证书")
        self.chk_ssl.setChecked(False)   # 默认关闭，方便自签环境
        opt_row.addWidget(self.chk_ssl)

        opt_row.addStretch(1)

        self.btn_start = QPushButton("🔍 开始查询")
        self.btn_start.setStyleSheet(self._btn_style("#1e3a8a", "white"))
        self.btn_start.clicked.connect(self.on_start)

        self.btn_stop = QPushButton("⏹️ 停止")
        self.btn_stop.setStyleSheet(self._btn_style("#b45309", "white"))
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)

        self.btn_sample = QPushButton("📋 示例")
        self.btn_sample.setStyleSheet(self._btn_style("#ffffff", "#1e2f44"))
        self.btn_sample.clicked.connect(self.on_sample)

        self.btn_clear = QPushButton("🗑️ 清空")
        self.btn_clear.setStyleSheet(self._btn_style("#ffffff", "#1e2f44"))
        self.btn_clear.clicked.connect(self.on_clear)

        opt_row.addWidget(self.btn_start)
        opt_row.addWidget(self.btn_stop)
        opt_row.addWidget(self.btn_sample)
        opt_row.addWidget(self.btn_clear)

        cfg_layout.addLayout(opt_row)
        root.addWidget(cfg)

        # ==== 中间：输入 + 表格 分栏 ====
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # 输入区
        input_box = QGroupBox("后缀 / IP 列表（每行一个，会拼接到基础URL后面）")
        input_box.setStyleSheet("QGroupBox{font-weight:600; color:#1e2f44; border:1px solid #e2e9f2;"
                                "border-radius:10px; margin-top:8px; padding-top:14px;}")
        in_lay = QVBoxLayout(input_box)
        self.ip_edit = QTextEdit()
        self.ip_edit.setPlaceholderText("每行一个，例如:\n8.8.8.8\n1.1.1.1\n114.114.114.114\nexample.com")
        self.ip_edit.setStyleSheet("font-family: Consolas, monospace; font-size: 13px;"
                                   "border:1.5px solid #dbe2eb; border-radius:8px; padding:8px;")
        self.ip_edit.setFixedHeight(140)
        in_lay.addWidget(self.ip_edit)

        self.lbl_count = QLabel("0 条")
        self.lbl_count.setStyleSheet("color:#1e4b8f; background:#e6f0ff; border-radius:12px;"
                                     "padding:2px 10px;")
        self.lbl_count.setAlignment(Qt.AlignRight)
        in_lay.addWidget(self.lbl_count)
        self.ip_edit.textChanged.connect(self._update_count)
        splitter.addWidget(input_box)

        # 表格区
        table_box = QGroupBox("查询结果")
        table_box.setStyleSheet("QGroupBox{font-weight:600; color:#1e2f44; border:1px solid #e2e9f2;"
                                "border-radius:10px; margin-top:8px; padding-top:14px;}")
        t_lay = QVBoxLayout(table_box)

        top_row = QHBoxLayout()
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color:#1e2f44; background:#eef2f6; padding:4px 12px;"
                                      "border-radius:12px;")
        top_row.addWidget(self.lbl_status)
        top_row.addStretch(1)

        self.btn_export = QPushButton("⬇️ 导出 CSV")
        self.btn_export.setStyleSheet(self._btn_style("#0e7c4a", "white"))
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.on_export)
        top_row.addWidget(self.btn_export)
        t_lay.addLayout(top_row)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setStyleSheet(
            "QProgressBar{border:1px solid #dbe2eb; border-radius:6px; background:#f1f5fb; height:16px;}"
            "QProgressBar::chunk{background:#3b82f6; border-radius:5px;}")
        t_lay.addWidget(self.progress)

        # 表格
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "后缀/IP", "完整URL", "HTTP状态", "耗时", "响应内容"])
        self.table.setStyleSheet(
            "QTableWidget{border:1px solid #e2e9f2; border-radius:8px; gridline-color:#ecf1f8;}"
            "QHeaderView::section{background:#f2f6fc; padding:6px; border:none;"
            "border-bottom:1px solid #d9e2ef; font-weight:600; color:#1e2f44;}"
            "QTableWidget::item{padding:4px;}"
            "QTableWidget::item:selected{background:#e6f0ff; color:#1e2f44;}")
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setColumnWidth(2, 300)
        t_lay.addWidget(self.table)
        splitter.addWidget(table_box)

        splitter.setSizes([180, 460])
        root.addWidget(splitter, 1)

        # 底部说明
        foot = QLabel("说明：每行内容会作为后缀拼接到基础URL发起 GET 请求。SSL 校验默认关闭，"
                      "若需严格校验请勾选。桌面程序无浏览器 CORS 限制。")
        foot.setStyleSheet("color:#6b7d99; font-size:12px;")
        foot.setWordWrap(True)
        root.addWidget(foot)

    def _btn_style(self, bg, fg):
        return (f"QPushButton{{background:{bg}; color:{fg}; border:1px solid #cddae9;"
                f"border-radius:18px; padding:8px 18px; font-weight:500;}}"
                f"QPushButton:hover{{background:#eaf1fb; color:#1e2f44;}}"
                f"QPushButton:disabled{{opacity:0.6;}}")

    # ---------- 事件 ----------
    def _update_count(self):
        n = len([l for l in self.ip_edit.toPlainText().splitlines() if l.strip()])
        self.lbl_count.setText(f"{n} 条")

    def on_sample(self):
        self.ip_edit.setPlainText(
            "8.8.8.8\n1.1.1.1\n114.114.114.114\n223.5.5.5\n180.76.76.76\nexample.com\ntest/path"
        )

    def on_clear(self):
        self.ip_edit.clear()
        self._reset_table()
        self.lbl_status.setText("就绪")
        self.progress.setValue(0)

    def _reset_table(self):
        self.table.setRowCount(0)
        self.results = []
        self.btn_export.setEnabled(False)

    def on_start(self):
        base = self.base_url_edit.text().strip()
        if not base:
            QMessageBox.warning(self, "提示", "请填写基础URL")
            return
        suffixes = [l.strip() for l in self.ip_edit.toPlainText().splitlines() if l.strip()]
        if not suffixes:
            QMessageBox.warning(self, "提示", "请至少输入一行后缀")
            return

        # 重置
        self._reset_table()
        self.current_suffixes = suffixes
        self.progress.setValue(0)
        self.progress.setRange(0, len(suffixes))
        self.progress.setFormat(f"%v / %m")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_export.setEnabled(False)
        self.lbl_status.setText("正在查询…")

        # 建表行
        self.table.setRowCount(len(suffixes))
        for i, s in enumerate(suffixes):
            self.table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(i, 1, QTableWidgetItem(s))
            for c in range(2, 6):
                self.table.setItem(i, c, QTableWidgetItem("…"))
        self.table.setUpdatesEnabled(True)

        # 启动线程
        self.worker = CheckWorker(
            base_url=base,
            suffixes=suffixes,
            concurrency=self.spin_concurrency.value(),
            timeout=self.spin_timeout.value(),
            verify_ssl=self.chk_ssl.isChecked(),
        )
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.row_done.connect(self._on_row_done)
        self.worker.finished_all.connect(self._on_finished)
        self.worker.status.connect(self.lbl_status.setText)
        self.worker_thread.start()

    def _on_progress(self, done, total):
        self.progress.setValue(done)
        self.lbl_status.setText(f"查询中… {done}/{total}")

    def _on_row_done(self, idx, res):
        self._fill_row(idx, res)
        # 保持滚动跟随（可选）
        if idx == self.table.rowCount() - 1:
            self.table.scrollToBottom()

    def _fill_row(self, row, res):
        status_text = str(res.get("status", ""))
        items = [
            (2, res.get("url", "")),
            (3, status_text),
            (4, res.get("elapsed", "-")),
            (5, res.get("body", "")),
        ]
        for col, text in items:
            item = QTableWidgetItem(str(text))
            item.setToolTip(str(text))
            # 状态颜色
            if col == 3:
                try:
                    code = int(status_text)
                    if 200 <= code < 300:
                        item.setForeground(QColor("#0b6b3f"))
                    elif code >= 400:
                        item.setForeground(QColor("#b81c1c"))
                except ValueError:
                    item.setForeground(QColor("#b81c1c"))
            self.table.setItem(row, col, item)

    def _on_finished(self, results):
        self.results = results
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(self.progress.maximum())
        self.btn_export.setEnabled(bool(results))

        ok = sum(1 for r in results if not r["error"] and isinstance(r["status"], int)
                 and 200 <= r["status"] < 300)
        self.lbl_status.setText(f"✅ 完成，共 {len(results)} 条，成功 {ok} 条")

        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
            self.worker_thread = None
            self.worker = None

    def on_stop(self):
        if self.worker:
            self.worker.stop()
            self.lbl_status.setText("⏹️ 正在停止…")
            self.btn_stop.setEnabled(False)

    def on_export(self):
        if not self.results:
            QMessageBox.information(self, "提示", "没有可导出的数据")
            return
        default = f"ip_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "保存CSV", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["序号", "后缀/IP", "完整URL", "HTTP状态", "耗时", "响应内容"])
                for i, r in enumerate(self.results, 1):
                    w.writerow([i, r["suffix"], r["url"], r["status"],
                                r["elapsed"], r["body"]])
            QMessageBox.information(self, "成功", f"已导出到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait()
        event.accept()


# ---------------- 入口 ----------------
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # 全局字体
    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()