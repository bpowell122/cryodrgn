API Reference
=============

For users simply looking to analyze their datasets with tomoDRGN, we recommend just using the :doc:`CLI commands <../command_usage/index>`.
Should you wish to dive deeper into tomoDRGN's source code, this section details the Python API of each tomoDRGN core module and command script.

TomoDRGN core
-------------
.. autosummary::
   :toctree: _autosummary
   :template: custom-module-template.rst
   :caption: TomoDRGN core modules

   tomodrgn.analysis
   tomodrgn.beta_schedule
   tomodrgn.config
   tomodrgn.convergence
   tomodrgn.ctf
   tomodrgn.dataset
   tomodrgn.dose
   tomodrgn.fft
   tomodrgn.lattice
   tomodrgn.lie_tools
   tomodrgn.losses
   tomodrgn.models
   tomodrgn.mrc
   tomodrgn.pose
   tomodrgn.set_transformer
   tomodrgn.so3_grid
   tomodrgn.starfile
   tomodrgn.utils


TomoDRGN commands
-----------------
.. autosummary::
   :toctree: _autosummary
   :template: custom-module-template.rst
   :caption: TomoDRGN commands

   tomodrgn.commands.analyze
   tomodrgn.commands.analyze_volumes
   tomodrgn.commands.backproject_voxel
   tomodrgn.commands.cleanup
   tomodrgn.commands.convergence_nn
   tomodrgn.commands.convergence_vae
   tomodrgn.commands.downsample
   tomodrgn.commands.eval_images
   tomodrgn.commands.eval_vol
   tomodrgn.commands.filter_star
   tomodrgn.commands.graph_traversal
   tomodrgn.commands.pc_traversal
   tomodrgn.commands.subtomo2chimerax
   tomodrgn.commands.train_nn
   tomodrgn.commands.train_vae
   tomodrgn.commands.view_config