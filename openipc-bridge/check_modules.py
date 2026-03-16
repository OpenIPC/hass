#!/usr/bin/env python3
"""Проверка установленных модулей внутри аддона"""
import sys
import importlib

def check_module(module_name):
    """Проверяет наличие модуля"""
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, '__version__', 'unknown')
        print(f"✅ {module_name} - OK (version: {version})")
        return True
    except ImportError as e:
        print(f"❌ {module_name} - {e}")
        return False

def main():
    """Главная функция"""
    print("="*50)
    print("Проверка Python модулей внутри аддона")
    print("="*50)
    
    modules = [
        "flask",
        "requests",
        "cv2",
        "numpy",
        "PIL",
        "pyzbar",
        "gtts"
    ]
    
    failed = []
    for module in modules:
        if not check_module(module):
            failed.append(module)
    
    print("="*50)
    if failed:
        print(f"❌ Провалено модулей: {len(failed)}")
        sys.exit(1)
    else:
        print("✅ Все модули загружены успешно!")
        sys.exit(0)

if __name__ == "__main__":
    main()