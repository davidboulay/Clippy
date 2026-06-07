# Clippy packaging targets.
.PHONY: help deb flatpak appimage arch mac-app clean

help:
	@echo "Targets:"
	@echo "  make deb       Build dist/clippy_<ver>_all.deb        (no network/sudo)"
	@echo "  make flatpak   Build & install the Flatpak            (needs network)"
	@echo "  make appimage  Build dist/Clippy-<ver>-<arch>.AppImage (experimental)"
	@echo "  make arch      Build an Arch package (runs makepkg)"
	@echo "  make mac-app   Build dist/Clippy.app                  (run ON a Mac)"
	@echo "  make clean     Remove build artifacts"

mac-app:
	./packaging/macos/build-app.sh

deb:
	./packaging/deb/build-deb.sh

flatpak:
	./packaging/flatpak/build-flatpak.sh

appimage:
	./packaging/appimage/build-appimage.sh

arch:
	cd packaging/arch && makepkg -f

clean:
	rm -rf dist build packaging/flatpak/build-dir packaging/flatpak/.flatpak-builder
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
