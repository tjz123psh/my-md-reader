blueprint_compiler = find_program('blueprint-compiler')

window_ui = custom_target(
  'window-ui',
  input: 'resources/ui/window.blp',
  output: 'window.ui',
  command: [
    blueprint_compiler,
    'compile',
    '--output', '@OUTPUT@',
    '@INPUT@',
  ],
  build_by_default: true,
)

resources = gnome.compile_resources(
  'mdreader',
  'resources/io.github.pang.mdreader.gresource.xml',
  dependencies: window_ui,
  source_dir: [meson.current_build_dir(), meson.current_source_dir() / 'resources'],
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
)

launcher = configure_file(
  input: 'md-reader.in',
  output: 'md-reader',
  configuration: {
    'PYTHON': python.full_path(),
  },
)

install_data(
  launcher,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

run_target(
  'run',
  command: [python, launcher],
)

python_install_dir = python.get_install_dir()
install_subdir(
  'mdreader',
  install_dir: python_install_dir,
  exclude_directories: [
    '__pycache__',
    'models/__pycache__',
    'services/__pycache__',
    'widgets/__pycache__',
  ],
)

meson.add_devenv({
  'PYTHONPATH': meson.current_source_dir(),
  'MDREADER_RESOURCE_FILE': resources.full_path(),
  'GSETTINGS_SCHEMA_DIR': meson.project_build_root() / 'data',
})
