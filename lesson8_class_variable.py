#!/usr/bin/env python3
"""
Lesson 8 补充：类变量示例
定义在 class 语句块内，所有实例共享。
"""

from __future__ import annotations


class Library:
    """
    图书馆类：记录图书总量（类变量），每本书有各自的书名（实例变量）。
    """
    total_books: int = 0               # 类变量，所有实例共享

    def __init__(self, title: str) -> None:
        self.title = title             # 实例变量，每本书独立
        Library.total_books += 1       # 通过类名修改类变量

    def info(self) -> str:
        return f"《{self.title}》 | 馆藏总量: {Library.total_books}"


def main() -> None:
    b1 = Library("Python编程")
    b2 = Library("设计模式")

    print(b1.info())   # Python编程 | 馆藏总量: 2
    print(b2.info())   # 设计模式   | 馆藏总量: 2

    # 类变量可通过类名或实例访问，但通过实例赋值会创建实例变量（不推荐）
    print(Library.total_books)   # 2
    b1.total_books = 100         # 这只在 b1 上创建了实例变量，不影响类变量
    print(b1.total_books)        # 100 （实例变量覆盖了类变量）
    print(b2.total_books)        # 2   （仍然访问类变量）
    print(Library.total_books)   # 2   （类变量未改变）


if __name__ == "__main__":
    main()
