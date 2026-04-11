FROM ubuntu:22.04

# Install dependencies
RUN apt-get update && \
    apt-get install -y curl ca-certificates sudo bash


# # Install kind
# RUN curl -Lo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64 \
#     && chmod +x /usr/local/bin/kind

# # Install kubectl
# RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
#     && chmod +x kubectl \
#     && mv kubectl /usr/local/bin/

# # For VS Code user
# RUN useradd -ms /bin/bash devuser && echo "devuser ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
# USER devuser
# WORKDIR /workspace
