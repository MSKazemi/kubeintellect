class KubeQ < Formula
  include Language::Python::Virtualenv

  desc "Interactive terminal CLI for chatting with your Kubernetes cluster via an AI backend"
  homepage "https://github.com/MSKazemi/kube_q"
  url "https://files.pythonhosted.org/packages/source/k/kube-q/kube_q-1.0.0.tar.gz"
  sha256 "0ecce3a15800343c3cbcd9490ce695e923b4390c0808629fcaec5ea03a9c967d"
  license "MIT"

  depends_on "python@3.12"

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.28.1.tar.gz"
    sha256 "75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-14.3.3.tar.gz"
    sha256 "5a5c2a4d5e8bbf946abbc1c86e8a69a7e0fb70d8e8c41e6aeeb3d08b7d6c4c3"
  end

  resource "prompt_toolkit" do
    url "https://files.pythonhosted.org/packages/source/p/prompt_toolkit/prompt_toolkit-3.0.51.tar.gz"
    sha256 "ee2b1d8a1de5c6f0a9e0b28c5c7b0e48dc3bedd0c9cbf7c62d79ad27012a41c"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/source/p/pygments/pygments-2.18.0.tar.gz"
    sha256 "786ff802f32e91311bff3889f6e9a86e81505fe99f2735bb6d60ae0c5004f199"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "kube-q", shell_output("#{bin}/kq --help 2>&1", 0)
  end
end
