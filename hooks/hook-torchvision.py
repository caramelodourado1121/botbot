from PyInstaller.utils.hooks import collect_dynamic_libs


binaries = collect_dynamic_libs("torchvision")
hiddenimports = ["torchvision", "torchvision.transforms"]