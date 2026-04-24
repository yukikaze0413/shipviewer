"""
工业模型查看器 - ShipViewer
基于 PySide6 + VTK 的工业级3D模型查看器
支持 .3dm (Rhino) 和 .glb (glTF) 格式
"""
import sys
import os

# 设置环境变量，确保高DPI支持
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QLocale
from PySide6.QtGui import QFont, QIcon

from core.main_window import MainWindow


def main():
    """应用程序入口"""
    app = QApplication(sys.argv)

    # 设置应用程序信息
    app.setApplicationName("工业模型查看器")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("ShipViewer")

    # 设置中文区域
    locale = QLocale(QLocale.Chinese, QLocale.China)
    QLocale.setDefault(locale)

    # 设置默认字体
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 加载样式表
    style_path = os.path.join(os.path.dirname(__file__), "styles", "dark_theme.qss")
    if os.path.exists(style_path):
        with open(style_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
