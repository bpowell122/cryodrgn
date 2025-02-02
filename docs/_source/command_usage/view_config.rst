tomodrgn view_config
===========================

Purpose
--------
View details of the configuration specified for a pretrained ``train_vae`` model.

Sample usage
------------
The examples below are adapted from ``tomodrgn/testing/commandtest*.py``, and rely on other outputs from ``commandtest.py`` to execute successfully.

.. code-block:: bash

    # Warp v1 style inputs
    tomodrgn \
        view_config \
        output/vae_both_sim_zdim2

    # WarpTools style inputs
    tomodrgn \
        view_config \
        output/vae_warptools_70S_zdim2

Arguments
---------

.. argparse::
   :ref: tomodrgn.commands.view_config.add_args
   :prog: view_config
   :nodescription:
   :noepilog:

Common next steps
------------------

* Highly variable, see next steps for ``tomodrgn train_vae``