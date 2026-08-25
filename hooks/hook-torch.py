from PyInstaller.utils.hooks import collect_dynamic_libs


binaries = collect_dynamic_libs("torch")
hiddenimports = ["torch", "torch.nn", "torch.nn.functional", "torch.jit"]
excludedimports = [
    "torch.distributed",
    "torch._inductor",
    "torch.utils.tensorboard",
    "torch.testing",
]