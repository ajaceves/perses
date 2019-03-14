"""
Unit tests for test utilities.

"""

__author__ = 'John D. Chodera'

################################################################################
# GLOBAL IMPORTS
################################################################################

from nose.plugins.attrib import attr

################################################################################
# Suppress matplotlib logging
################################################################################

import logging
mpl_logger = logging.getLogger('matplotlib')
mpl_logger.setLevel(logging.WARNING)

################################################################################
# CONSTANTS
################################################################################

################################################################################
# TESTS
################################################################################

def test_createOEMolFromIUPAC():
    """Test createOEMolFromIUPAC"""
    from perses.tests.utils import createOEMolFromIUPAC

    # Create a few molecules
    iupac_list = [
        'ethane',
        'phenol',
        'aspirin',
    ]
    for iupac in iupac_list:
        oemol = createOEMolFromIUPAC(iupac)

    # Test setting the title
    oemol = createOEMolFromIUPAC('ethane', title='XYZ')
    assert oemol.GetTitle() == 'XYZ'

def test_createOEMolFromSMILES():
    """Test createOEMolFromSMILES"""
    from perses.tests.utils import createOEMolFromSMILES

    # Create a few molecules
    smiles_list = [
        'CC', # ethane
        'c1ccc(cc1)O', # phenol
        'O=C(C)Oc1ccccc1C(=O)O', # aspirin
    ]
    for smiles in smiles_list:
        oemol = createOEMolFromSMILES(smiles)

    # Test setting the title
    oemol = createOEMolFromSMILES('CC', title='XYZ')
    assert oemol.GetTitle() == 'XYZ'

def test_createSystemFromIUPAC():
    """Test createSystemFromIUPAC"""
    from perses.tests.utils import createSystemFromIUPAC

    # Create a few molecules
    iupac_list = [
        'ethane',
        'phenol',
        'aspirin',
    ]
    for iupac in iupac_list:
        oemol, system, positions, topology = createSystemFromIUPAC(iupac)
        resnames = [ residue.name for residue in topology.residues() ]
        assert resnames[0] == 'MOL'

    # Test setting the residue name
    oemol, system, positions, topology = createSystemFromIUPAC('ethane', resname='XYZ')
    resnames = [ residue.name for residue in topology.residues() ]
    assert resnames[0] == 'XYZ'