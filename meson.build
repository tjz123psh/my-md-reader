project(
  'mdreader',
  version: '0.1.0',
  meson_version: '>= 1.2.0',
)

gnome = import('gnome')
python = import('python').find_installation('python3')

app_id = 'io.github.pang.mdreader'
pkgdatadir = get_option('prefix') / get_option('datadir') / 'mdreader'

subdir('data')
subdir('src')
subdir('tests')
