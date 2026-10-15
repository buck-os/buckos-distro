Name: sdk
Version: 1.0
Release: 1
Summary: Static SDK fixture for RPM payload bundles
License: GPL-2.0-only

%description
A tiny static archive and header to exercise extraction and downstream linking.

%prep

%build
printf 'int fixture_answer(void) { return 42; }\n' > fixture.c
gcc -c -O2 -o fixture.o fixture.c
ar crD libfixture.a fixture.o

%install
mkdir -p %{buildroot}/opt/buckos-sdk/lib %{buildroot}/opt/buckos-sdk/include
cp libfixture.a %{buildroot}/opt/buckos-sdk/lib/
printf 'int fixture_answer(void);\n' > %{buildroot}/opt/buckos-sdk/include/fixture.h
ln -s /opt/buckos-sdk/lib/libfixture.a %{buildroot}/opt/buckos-sdk/lib/libalias.a

%files
/opt/buckos-sdk
