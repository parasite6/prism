"""BiRefNet (Peng Zheng et al., https://github.com/ZhengPeng7/BiRefNet), MIT license.

birefnet.py and BiRefNet_config.py are the files published with the
BiRefNet_HR-matting weights (huggingface.co/ZhengPeng7/BiRefNet_HR-matting).
The only change is their imports, which point at _compat.py instead of
transformers and timm. The weights aren't included: matting.py loads them from
~/.local/share/prism-models/ZhengPeng7/model.safetensors.
"""
