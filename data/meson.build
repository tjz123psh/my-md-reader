install_data(
  app_id + '.desktop',
  install_dir: get_option('datadir') / 'applications',
)

install_data(
  app_id + '.metainfo.xml',
  install_dir: get_option('datadir') / 'metainfo',
)

install_data(
  app_id + '.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

install_data(
  'icons/hicolor/scalable/apps/' + app_id + '.svg',
  install_dir: get_option('datadir') / 'icons/hicolor/scalable/apps',
)

install_data(
  'icons/hicolor/symbolic/apps/' + app_id + '-symbolic.svg',
  install_dir: get_option('datadir') / 'icons/hicolor/symbolic/apps',
)

gnome.compile_schemas(
  depend_files: files(app_id + '.gschema.xml'),
)

desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test(
    'validate-desktop-file',
    desktop_file_validate,
    args: files(app_id + '.desktop'),
  )
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test(
    'validate-appstream',
    appstreamcli,
    args: [
      'validate',
      '--no-net',
      files(app_id + '.metainfo.xml'),
    ],
  )
endif
