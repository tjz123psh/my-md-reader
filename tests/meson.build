test_env = environment()
test_env.set('PYTHONPATH', meson.project_source_root() / 'src')

test(
  'python-unit-tests',
  python,
  args: ['-m', 'unittest', 'discover', '-s', meson.current_source_dir(), '-v'],
  env: test_env,
)

smoke_env = environment()
smoke_env.set('PYTHONPATH', meson.project_source_root() / 'src')
smoke_env.set('MDREADER_RESOURCE_FILE', resources.full_path())
smoke_env.set('GSETTINGS_SCHEMA_DIR', meson.project_build_root() / 'data')

test(
  'gtk-smoke',
  python,
  args: [
    files('smoke_app.py'),
    launcher,
    files('fixtures/reader-sample.md'),
  ],
  env: smoke_env,
  is_parallel: false,
  timeout: 45,
)
