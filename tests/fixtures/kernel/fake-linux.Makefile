RELEASE := 6.18.0-kbuild-$(if $(filter arm64,$(ARCH)),aarch64,x86_64)

.PHONY: olddefconfig bzImage Image vmlinux modules kernelrelease modules_install

olddefconfig:
	@test -f $(O)/.config

bzImage:
	@mkdir -p $(O)/arch/x86/boot
	@printf kernel > $(O)/arch/x86/boot/bzImage

Image:
	@mkdir -p $(O)/arch/arm64/boot
	@printf kernel > $(O)/arch/arm64/boot/Image

vmlinux:
	@printf elf > $(O)/vmlinux

modules:
	@printf map > $(O)/System.map
	@printf symvers > $(O)/Module.symvers

kernelrelease:
	@printf %s $(RELEASE)

modules_install:
	@mkdir -p $(INSTALL_MOD_PATH)/lib/modules/$(RELEASE)/kernel
	@printf module > $(INSTALL_MOD_PATH)/lib/modules/$(RELEASE)/kernel/example.ko
	@printf 'kernel/example.ko:\n' > $(INSTALL_MOD_PATH)/lib/modules/$(RELEASE)/modules.dep
