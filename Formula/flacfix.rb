class Flacfix < Formula
  include Language::Python::Virtualenv

  desc "Tidy FLAC albums: tags, track names, cover art and Finder icons"
  homepage "https://github.com/OWNER/homebrew-flacfix"
  url "https://github.com/OWNER/homebrew-flacfix.git", tag: "v1.0"
  version "1.0"
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

  resource "mutagen" do
    url "https://files.pythonhosted.org/packages/df/70/1675da133ea92227da41bf5b24e1c66be597ff736a1533ade41da986852f/mutagen-1.48.1.tar.gz"
    sha256 "8f95637ab9f6f305cec6bd1294e197debe207998e3e068596563c74f86b0a173"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/51/33/4cd27639502f010084bcef7a2036391dd637439901341ce1a530f36f6e51/certifi-2026.11.20.tar.gz"
    sha256 "af1cb240a8133f105aa52684a3a3258339d7f2fd0bd4e2812698ff5cf5588c4e"
  end

  def install
    venv = virtualenv_create(libexec/"venv", "python3.14")
    venv.pip_install resources

    # Finder-icon helper (needs the Xcode command line tools, which Homebrew requires anyway)
    system "swiftc", "-O", "seticon.swift", "-o", "seticon"

    libexec.install "flacfix.py", "seticon"
    pkgshare.install "FlacFix-Guide.txt"

    (bin/"FlacFix").write <<~SH
      #!/bin/sh
      exec "#{libexec}/venv/bin/python" "#{libexec}/flacfix.py" "$@"
    SH
    chmod 0755, bin/"FlacFix"
  end

  test do
    assert_match "FlacFix Version", shell_output("#{bin}/FlacFix")
    assert_match "--Convert", shell_output("#{bin}/FlacFix")
    assert_predicate libexec/"seticon", :executable?
  end
end
