"""
This contains the base class for the geometry engine, which proposes new positions
for each additional atom that must be added.
"""
from simtk import unit

import numpy as np
import collections
import functools

from perses.storage import NetCDFStorage, NetCDFStorageView

################################################################################
# Initialize logging
################################################################################

import logging
_logger = logging.getLogger("geometry")

################################################################################
# Suppress matplotlib logging
################################################################################

mpl_logger = logging.getLogger('matplotlib')
mpl_logger.setLevel(logging.WARNING)


################################################################################
# Constants
################################################################################

LOG_ZERO = -1.0e+6

################################################################################
# Utility methods
################################################################################

def check_dimensionality(quantity, compatible_units):
    """
    Ensure that the specified quantity has units compatible with specified unit.

    Parameters
    ----------
    quantity : simtk.unit.Quantity or float
        The quantity to be checked
    compatible_units : simtk.unit.Quantity or simtk.unit.Unit or float
        Ensure ``quantity`` is either float or numpy array (if ``float`` specified) or is compatible with the specified units

    Raises
    ------
    ValueError if the specified quantity does not have the appropriate dimensionality or type

    Returns
    -------
    is_compatible : bool
        Returns True if dimensionality is as requested

    """
    if unit.is_quantity(compatible_units) or unit.is_unit(compatible_units):
        from simtk.unit.quantity import is_dimensionless
        if not is_dimensionless(quantity / compatible_units):
            raise ValueError('{} does not have units compatible with expected {}'.format(quantity, compatible_units))
    elif compatible_units == float:
        if not (isinstance(quantity, float) or isinstance(quantity, np.ndarray)):
            raise ValueError("'{}' expected to be a float, but was instead {}".format(quantity, type(quantity)))
    else:
        raise ValueError("Don't know how to handle compatible_units of {}".format(compatible_units))

    # Units are compatible if they pass this point
    return True

class GeometryEngine(object):
    """
    This is the base class for the geometry engine.

    Arguments
    ---------
    metadata : dict
        GeometryEngine-related metadata as a dict
    """

    def __init__(self, metadata=None, storage=None):
        # TODO: Either this base constructor should be called by subclasses, or we should remove its arguments.
        pass

    def propose(self, top_proposal, current_positions, beta):
        """
        Make a geometry proposal for the appropriate atoms.

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        beta : float
            The inverse temperature

        Returns
        -------
        new_positions : [n, 3] ndarray
            The new positions of the system
        """
        return np.array([0.0,0.0,0.0])

    def logp_reverse(self, top_proposal, new_coordinates, old_coordinates, beta):
        """
        Calculate the logp for the given geometry proposal

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        new_coordinates : [n, 3] np.ndarray
            The coordinates of the system after the proposal
        old_coordiantes : [n, 3] np.ndarray
            The coordinates of the system before the proposal
        direction : str, either 'forward' or 'reverse'
            whether the transformation is for the forward NCMC move or the reverse
        beta : float
            The inverse temperature

        Returns
        -------
        logp : float
            The log probability of the proposal for the given transformation
        """
        return 0.0


class FFAllAngleGeometryEngine(GeometryEngine):
    """
    This is an implementation of GeometryEngine which uses all valence terms and OpenMM

    Parameters
    ----------
    use_sterics : bool, optional, default=False
        If True, sterics will be used in proposals to minimize clashes.
        This may significantly slow down the simulation, however.

    """
    def __init__(self, metadata=None, use_sterics=False, n_bond_divisions=1000, n_angle_divisions=180, n_torsion_divisions=360, verbose=True, storage=None, bond_softening_constant=1.0, angle_softening_constant=1.0):
        self._metadata = metadata
        self.write_proposal_pdb = False # if True, will write PDB for sequential atom placements
        self.pdb_filename_prefix = 'geometry-proposal' # PDB file prefix for writing sequential atom placements
        self.nproposed = 0 # number of times self.propose() has been called
        self.verbose = verbose
        self.use_sterics = use_sterics
        self._n_bond_divisions = n_bond_divisions
        self._n_angle_divisions = n_angle_divisions
        self._n_torsion_divisions = n_torsion_divisions
        self._bond_softening_constant = bond_softening_constant
        self._angle_softening_constant = angle_softening_constant
        if storage:
            self._storage = NetCDFStorageView(modname="GeometryEngine", storage=storage)
        else:
            self._storage = None

    def propose(self, top_proposal, current_positions, beta):
        """
        Make a geometry proposal for the appropriate atoms.

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        current_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The current positions
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy

        Returns
        -------
        new_positions : [n, 3] ndarray
            The new positions of the system
        logp_proposal : float
            The log probability of the forward-only proposal
        """
        # Ensure positions have units compatible with nanometers
        check_dimensionality(current_positions, unit.nanometers)
        check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # TODO: Change this to use md_unit_system instead of hard-coding nanometers
        if not top_proposal.unique_new_atoms:
            # If there are no unique new atoms, return new positions in correct order for new topology object and log probability of zero
            # TODO: Carefully check this
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.old_topology, top_proposal.old_system)
            atoms_with_positions = [ structure.atoms[atom_idx] for atom_idx in top_proposal.new_to_old_atom_map.keys() ]
            new_positions = self._copy_positions(atoms_with_positions, top_proposal, current_positions)
            logp_proposal = 0.0
        else:
            logp_proposal, new_positions = self._logp_propose(top_proposal, current_positions, beta, direction='forward')
            self.nproposed += 1

        check_dimensionality(new_positions, unit.nanometers)
        check_dimensionality(logp_proposal, float)
        return new_positions, logp_proposal


    def logp_reverse(self, top_proposal, new_coordinates, old_coordinates, beta):
        """
        Calculate the logp for the given geometry proposal

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        new_coordinates : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system after the proposal
        old_coordiantes : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system before the proposal
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy

        Returns
        -------
        logp : float
            The log probability of the proposal for the given transformation
        """
        check_dimensionality(new_coordinates, unit.nanometers)
        check_dimensionality(old_coordinates, unit.nanometers)
        check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # If there are no unique old atoms, the log probability is zero.
        if not top_proposal.unique_old_atoms:
            return 0.0

        # Compute log proposal probability for reverse direction
        logp_proposal, _ = self._logp_propose(top_proposal, old_coordinates, beta, new_positions=new_coordinates, direction='reverse')

        check_dimensionality(logp_proposal, float)
        return logp_proposal

    def _write_partial_pdb(self, pdbfile, topology, positions, atoms_with_positions, model_index):
        """
        Write the subset of the molecule for which positions are defined.

        Parameters
        ----------
        pdbfile : file-like object
            The open file-like object for the PDB file being written
        topology : simtk.openmm.Topology
            The OpenMM Topology object
        positions : simtk.unit.Quantity of shape (n_atoms, 3) with units compatible with nanometers
            The positions
        atoms_with_positions : list of parmed.Atom
            parmed Atom objects for which positions have been defined
        model_index : int
            The MODEL index for the PDB file to use

        """
        check_dimensionality(positions, unit.nanometers)

        from simtk.openmm.app import Modeller
        modeller = Modeller(topology, positions)
        atom_indices_with_positions = [ atom.idx for atom in atoms_with_positions ]
        atoms_to_delete = [ atom for atom in modeller.topology.atoms() if (atom.index not in atom_indices_with_positions) ]
        modeller.delete(atoms_to_delete)

        pdbfile.write('MODEL %5d\n' % model_index)
        from simtk.openmm.app import PDBFile
        PDBFile.writeFile(modeller.topology, modeller.positions, file=pdbfile)
        pdbfile.flush()
        pdbfile.write('ENDMDL\n')

    def _logp_propose(self, top_proposal, old_positions, beta, new_positions=None, direction='forward'):
        """
        This is an INTERNAL function that handles both the proposal and the logp calculation,
        to reduce code duplication. Whether it proposes or just calculates a logp is based on
        the direction option. Note that with respect to "new" and "old" terms, "new" will always
        mean the direction we are proposing (even in the reverse case), so that for a reverse proposal,
        this function will still take the new coordinates as new_coordinates

        Parameters
        ----------
        top_proposal : topology_proposal.TopologyProposal object
            topology proposal containing the relevant information
        old_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system before the proposal
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers, optional, default=None
            The coordinates of the system after the proposal, or None for forward proposals
        direction : str
            Whether to make a proposal ('forward') or just calculate logp ('reverse')

        Returns
        -------
        logp_proposal : float
            the logp of the proposal
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The new positions (same as input if direction='reverse')
        """
        # Ensure all parameters have the expected units
        check_dimensionality(old_positions, unit.angstroms)
        if new_positions is not None:
            check_dimensionality(new_positions, unit.angstroms)

        # TODO: Overhaul the use of ProposalOrderTools to instead use ValenceProposalOrderTools
        proposal_order_tool = NetworkXProposalOrder(top_proposal, direction=direction)

        # Render into order in which atoms are to be grown
        atom_proposal_order = [ torsion[-1] for torsion in torsion_proposal_order ]

        growth_parameter_name = 'growth_stage'
        if direction=="forward":
            atom_proposal_order, logp_choice = proposal_order_tool.determine_proposal_order(direction='forward')

            # Find and copy known positions to match new topology
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.new_topology, top_proposal.new_system)
            atoms_with_positions = [structure.atoms[atom_idx] for atom_idx in top_proposal.new_to_old_atom_map.keys()]
            new_positions = self._copy_positions(atoms_with_positions, top_proposal, old_positions)

            # Create modified System object
            growth_system_generator = GeometrySystemGenerator(top_proposal.new_system, atom_proposal_order.keys(), global_parameter_name=growth_parameter_name, reference_topology=top_proposal.new_topology, use_sterics=self.use_sterics)
            growth_system = growth_system_generator.get_modified_system()

        elif direction=='reverse':
            if new_positions is None:
                raise ValueError("For reverse proposals, new_positions must not be none.")

            atom_proposal_order, logp_choice = proposal_order_tool.determine_proposal_order(direction='reverse')

            # Find and copy known positions to match old topology
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.old_topology, top_proposal.old_system)
            atoms_with_positions = [structure.atoms[atom_idx] for atom_idx in top_proposal.old_to_new_atom_map.keys()]

            # Create modified System object
            growth_system_generator = GeometrySystemGenerator(top_proposal.old_system, atom_proposal_order.keys(), global_parameter_name=growth_parameter_name, reference_topology=top_proposal.old_topology, use_sterics=self.use_sterics)
            growth_system = growth_system_generator.get_modified_system()
        else:
            raise ValueError("Parameter 'direction' must be forward or reverse")

        logp_proposal = logp_choice

        if self._storage:
            self._storage.write_object("{}_proposal_order".format(direction), proposal_order_tool, iteration=self.nproposed)

        if self.use_sterics:
            platform_name = 'CPU' # faster when sterics are in use
        else:
            platform_name = 'Reference' # faster when only valence terms are in use

        # Create an OpenMM context
        from simtk import openmm
        platform = openmm.Platform.getPlatformByName(platform_name)
        integrator = openmm.VerletIntegrator(1*unit.femtoseconds)
        context = openmm.Context(growth_system, integrator, platform)
        growth_system_generator.set_growth_parameter_index(len(atom_proposal_order)+1, context)
        growth_parameter_value = 1

        # Place each atom in predetermined order
        logging.debug("There are %d new atoms" % len(atom_proposal_order.items()))
        atom_placements = list()
        for atom, torsion in atom_proposal_order.items():
            bond_atom, angle_atom, torsion_atom = torsion.atom2, torsion.atom3, torsion.atom4

            # Activate the new atom interactions
            growth_system_generator.set_growth_parameter_index(growth_parameter_value, context=context)
            if self.verbose: _logger.info("Proposing atom %s from torsion %s" %(str(atom), str(torsion)))

            if atom != torsion.atom1:
                raise Exception('atom != torsion.atom1')

            # Get internal coordinates if direction is reverse
            if direction=='reverse':
                atom_coords = old_positions[atom.idx]
                bond_coords = old_positions[bond_atom.idx]
                angle_coords = old_positions[angle_atom.idx]
                torsion_coords = old_positions[torsion_atom.idx]
                internal_coordinates, detJ = self._cartesian_to_internal(atom_coords, bond_coords, angle_coords, torsion_coords)
                # Extract dimensionless internal coordinates
                r, theta, phi = internal_coordinates[0], internal_coordinates[1], internal_coordinates[2] # dimensionless

            bond = self._get_relevant_bond(atom, bond_atom)
            if bond is not None:
                if direction=='forward':
                    r = self._propose_bond(bond, beta, self._n_bond_divisions)

                logp_r = self._bond_logp(r, bond, beta, self._n_bond_divisions)
            else:
                if direction == 'forward':
                    constraint = self._get_bond_constraint(atom, bond_atom, top_proposal.new_system)
                    if constraint is None:
                        raise ValueError("Structure contains a topological bond [%s - %s] with no constraint or bond information." % (str(atom), str(bond_atom)))

                    r = constraint.value_in_unit_system(unit.md_unit_system) #set bond length to exactly constraint
                logp_r = 0.0

            # Propose an angle and calculate its log probability
            angle = self._get_relevant_angle(atom, bond_atom, angle_atom)
            if direction=='forward':
                theta = self._propose_angle(angle, beta, self._n_angle_divisions)

            logp_theta = self._angle_logp(theta, angle, beta, self._n_angle_divisions)

            # Propose a torsion angle and calcualate its log probability
            if direction=='forward':
                # Note that (r, theta) are dimensionless here
                phi, logp_phi = self._propose_torsion(context, torsion, new_positions, r, theta, beta, self._n_torsion_divisions)
                xyz, detJ = self._internal_to_cartesian(new_positions[bond_atom.idx], new_positions[angle_atom.idx], new_positions[torsion_atom.idx], r, theta, phi)
                new_positions[atom.idx] = xyz
            else:
                import copy
                old_positions_for_torsion = copy.deepcopy(old_positions)
                # Note that (r, theta, phi) are dimensionless here
                logp_phi = self._torsion_logp(context, torsion, old_positions_for_torsion, r, theta, phi, beta, self._n_torsion_divisions)

            #accumulate logp
            #if direction == 'reverse':
            if self.verbose: _logger.info('%8d logp_r %12.3f | logp_theta %12.3f | logp_phi %12.3f | log(detJ) %12.3f' % (atom.idx, logp_r, logp_theta, logp_phi, np.log(detJ)))

            atom_placement_array = np.array([atom.idx,
                                             r, theta, phi,
                                             logp_r, logp_theta, logp_phi, np.log(detJ)])
            atom_placements.append(atom_placement_array)

            logp_proposal += logp_r + logp_theta + logp_phi - np.log(detJ) # TODO: Check sign of detJ
            growth_parameter_value += 1

            # DEBUG: Write PDB file for placed atoms
            atoms_with_positions.append(atom)

        # Clean up OpenMM Context since garbage collector is sometimes slow
        del context

        #use a new array for each placement, since the variable size will be different.
        if self._storage:
            self._storage.write_array("atom_placement_logp_{}_{}".format(direction, self.nproposed), np.stack(atom_placements))

        check_dimensionality(logp_proposal, float)
        check_dimensionality(new_positions, unit.nanometers)
        return logp_proposal, new_positions

    def _copy_positions(self, atoms_with_positions, top_proposal, current_positions):
        """
        Copy the current positions to an array that will also hold new positions
        Parameters
        ----------
        atoms_with_positions : list of parmed.Atom
            parmed Atom objects denoting atoms that currently have positions
        top_proposal : topology_proposal.TopologyProposal
            topology proposal object
        current_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            Positions of the current system

        Returns
        -------
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            New positions for new topology object with known positions filled in
        """
        check_dimensionality(current_positions, unit.nanometers)

        # Create new positions
        new_shape = [top_proposal.n_atoms_new, 3]
        # Workaround for CustomAngleForce NaNs: Create random non-zero positions for new atoms.
        new_positions = unit.Quantity(np.random.random(new_shape), unit=unit.nanometers)

        # Copy positions for atoms that have them defined
        for atom in atoms_with_positions:
            old_index = top_proposal.new_to_old_atom_map[atom.idx]
            new_positions[atom.idx] = current_positions[old_index]

        check_dimensionality(new_positions, unit.nanometers)
        return new_positions

    def _get_relevant_bond(self, atom1, atom2):
        """
        Get parmaeters defining the bond connecting two atoms

        Parameters
        ----------
        atom1 : parmed.Atom
             One of the atoms in the bond
        atom2 : parmed.Atom object
             The other atom in the bond

        Returns
        -------
        bond : parmed.Bond with units modified to simtk.unit.Quantity
            Bond connecting the two atoms, or None if constrained or no bond term exists.
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.
        """
        bonds_1 = set(atom1.bonds)
        bonds_2 = set(atom2.bonds)
        relevant_bond_set = bonds_1.intersection(bonds_2)
        relevant_bond = relevant_bond_set.pop()
        if relevant_bond.type is None:
            return None
        relevant_bond_with_units = self._add_bond_units(relevant_bond)

        check_dimensionality(relevant_bond_with_units.type.req, unit.nanometers)
        check_dimensionality(relevant_bond_with_units.type.k, unit.kilojoules_per_mole/unit.nanometers**2)
        return relevant_bond_with_units

    def _get_bond_constraint(self, atom1, atom2, system):
        """
        Get constraint parameters corresponding to the bond between the given atoms

        Parameters
        ----------
        atom1 : parmed.Atom
           The first atom of the constrained bond
        atom2 : parmed.Atom
           The second atom of the constrained bond
        system : openmm.System
           The system containing the constraint

        Returns
        -------
        constraint : simtk.unit.Quantity or None
            If a constraint is defined between the two atoms, the length is returned; otherwise None
        """
        # TODO: This algorithm is incredibly inefficient.
        # Instead, generate a dictionary lookup of constrained distances.

        atom_indices = set([atom1.idx, atom2.idx])
        n_constraints = system.getNumConstraints()
        constraint = None
        for i in range(n_constraints):
            p1, p2, length = system.getConstraintParameters(i)
            constraint_atoms = set([p1, p2])
            if len(constraint_atoms.intersection(atom_indices))==2:
                constraint = length

        if constraint is not None:
            check_dimensionality(constraint, unit.nanometers)
        return constraint

    def _get_relevant_angle(self, atom1, atom2, atom3):
        """
        Get the angle containing the 3 given atoms

        Parameters
        ----------
        atom1 : parmed.Atom
            The first atom defining the angle
        atom2 : parmed.Atom
            The second atom defining the angle
        atom3 : parmed.Atom
            The third atom in the angle

        Returns
        -------
        relevant_angle_with_units : parmed.Angle with parmeters modified to be simtk.unit.Quantity
            Angle connecting the three atoms
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.
        """
        atom1_angles = set(atom1.angles)
        atom2_angles = set(atom2.angles)
        atom3_angles = set(atom3.angles)
        relevant_angle_set = atom1_angles.intersection(atom2_angles, atom3_angles)

        # DEBUG
        if len(relevant_angle_set) == 0:
            print('atom1_angles:')
            print(atom1_angles)
            print('atom2_angles:')
            print(atom2_angles)
            print('atom3_angles:')
            print(atom3_angles)
            raise Exception('Atoms %s-%s-%s do not share a parmed Angle term' % (atom1, atom2, atom3))

        relevant_angle = relevant_angle_set.pop()
        if type(relevant_angle.type.k) != unit.Quantity:
            relevant_angle_with_units = self._add_angle_units(relevant_angle)
        else:
            relevant_angle_with_units = relevant_angle

        check_dimensionality(relevant_angle.type.theteq, unit.radians)
        check_dimensionality(relevant_angle.type.k, unit.kilojoules_per_mole/unit.radians**2)
        return relevant_angle_with_units

    def _add_bond_units(self, bond):
        """
        Attach units to a parmed harmonic bond

        Parameters
        ----------
        bond : parmed.Bond
            The bond object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        bond : parmed.Bond with units modified to simtk.unit.Quantity
            The same modified Bond object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(bond.type.k)==unit.Quantity:
            return bond
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        bond.type.req = unit.Quantity(bond.type.req, unit=unit.angstrom)
        bond.type.k = unit.Quantity(2.0*bond.type.k, unit=unit.kilocalorie_per_mole/unit.angstrom**2)
        return bond

    def _add_angle_units(self, angle):
        """
        Attach units to parmed harmonic angle

        Parameters
        ----------
        angle : parmed.Angle
            The angle object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        angle : parmed.Angle with units modified to simtk.unit.Quantity
            The same modified Angle object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(angle.type.k)==unit.Quantity:
            return angle
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        angle.type.theteq = unit.Quantity(angle.type.theteq, unit=unit.degree)
        angle.type.k = unit.Quantity(2.0*angle.type.k, unit=unit.kilocalorie_per_mole/unit.radian**2)
        return angle

    def _add_torsion_units(self, torsion):
        """
        Add the correct units to a torsion

        Parameters
        ----------
        torsion : parmed.Torsion
            The angle object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        torsion : parmed.Torsion with units modified to simtk.unit.Quantity
            The same modified Torsion object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(torsion.type.phi_k) == unit.Quantity:
            return torsion
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        torsion.type.phi_k = unit.Quantity(torsion.type.phi_k, unit=unit.kilocalorie_per_mole)
        torsion.type.phase = unit.Quantity(torsion.type.phase, unit=unit.degree)
        return torsion

    def _rotation_matrix(self, axis, angle):
        """
        Compute a rotation matrix about the origin given a coordinate axis and an angle.

        Parameters
        ----------
        axis : ndarray of shape (3,) without units
            The axis about which rotation should occur
        angle : float (implicitly in radians)
            The angle of rotation about the axis

        Returns
        -------
        rotation_matrix : ndarray of shape (3,3) without units
            The 3x3 rotation matrix
        """
        axis = axis/np.linalg.norm(axis)
        axis_squared = np.square(axis)
        cos_angle = np.cos(angle)
        sin_angle = np.sin(angle)
        rot_matrix_row_one = np.array([cos_angle+axis_squared[0]*(1-cos_angle),
                                       axis[0]*axis[1]*(1-cos_angle) - axis[2]*sin_angle,
                                       axis[0]*axis[2]*(1-cos_angle)+axis[1]*sin_angle])

        rot_matrix_row_two = np.array([axis[1]*axis[0]*(1-cos_angle)+axis[2]*sin_angle,
                                      cos_angle+axis_squared[1]*(1-cos_angle),
                                      axis[1]*axis[2]*(1-cos_angle) - axis[0]*sin_angle])

        rot_matrix_row_three = np.array([axis[2]*axis[0]*(1-cos_angle)-axis[1]*sin_angle,
                                        axis[2]*axis[1]*(1-cos_angle)+axis[0]*sin_angle,
                                        cos_angle+axis_squared[2]*(1-cos_angle)])

        rotation_matrix = np.array([rot_matrix_row_one, rot_matrix_row_two, rot_matrix_row_three])
        return rotation_matrix

    def _cartesian_to_internal(self, atom_position, bond_position, angle_position, torsion_position):
        """
        Cartesian to internal coordinate conversion

        Parameters
        ----------
        atom_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom whose internal coordinates are to be computed with respect to other atoms
        bond_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with bond length ``r``
        angle_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with angle ``theta``
        torsion_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with torsion ``phi``

        Returns
        -------
        internal_coords : tuple of (float, float, float)
            Tuple representing (r, theta, phi):
            r : float (implicitly in nanometers)
                Bond length distance from ``bond_position`` to newly placed atom
            theta : float (implicitly in radians on domain [0,pi])
                Angle formed by ``(angle_position, bond_position, new_atom)``
            phi : float (implicitly in radians on domain [-pi, +pi))
                Torsion formed by ``(torsion_position, angle_position, bond_position, new_atom)``
        detJ : float
            The absolute value of the determinant of the Jacobian transforming from (r,theta,phi) to (x,y,z)
            .. todo :: Clarify the direction of the Jacobian

        """
        # TODO: _cartesian_to_internal and _internal_to_cartesian should accept/return units and have matched APIs

        check_dimensionality(atom_position, unit.nanometers)
        check_dimensionality(bond_position, unit.nanometers)
        check_dimensionality(angle_position, unit.nanometers)
        check_dimensionality(torsion_position, unit.nanometers)

        # Convert to internal coordinates once everything is dimensionless
        # Make sure positions are float64 arrays implicitly in units of nanometers for numba
        from perses.rjmc import coordinate_numba
        internal_coords = coordinate_numba.cartesian_to_internal(
            atom_position.value_in_unit(unit.nanometers).astype(np.float64),
            bond_position.value_in_unit(unit.nanometers).astype(np.float64),
            angle_position.value_in_unit(unit.nanometers).astype(np.float64),
            torsion_position.value_in_unit(unit.nanometers).astype(np.float64))
        # Return values are also in floating point implicitly in nanometers and radians
        r, theta, phi = internal_coords

        # Compute absolute value of determinant of Jacobian
        detJ = np.abs(r**2*np.sin(theta))

        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)
        check_dimensionality(detJ, float)

        return internal_coords, detJ

    def _internal_to_cartesian(self, bond_position, angle_position, torsion_position, r, theta, phi):
        """
        Calculate the cartesian coordinates of a newly placed atom in terms of internal coordinates,
        along with the absolute value of the determinant of the Jacobian.

        Parameters
        ----------
        bond_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with bond length ``r``
        angle_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with angle ``theta``
        torsion_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with torsion ``phi``
        r : simtk.unit.Quantity with units compatible with nanometers
            Bond length distance from ``bond_position`` to newly placed atom
        theta : simtk.unit.Quantity with units compatible with radians
            Angle formed by ``(angle_position, bond_position, new_atom)``
        phi : simtk.unit.Quantity with units compatible with radians
            Torsion formed by ``(torsion_position, angle_position, bond_position, new_atom)``

        Returns
        -------
        xyz : simtk.unit.Quantity wrapped numpy array of shape (3,) with units compatible with nanometers
            The position of the newly placed atom
        detJ : float
            The absolute value of the determinant of the Jacobian transforming from (r,theta,phi) to (x,y,z)
            .. todo :: Clarify the direction of the Jacobian

        """
        # TODO: _cartesian_to_internal and _internal_to_cartesian should accept/return units and have matched APIs

        check_dimensionality(bond_position, unit.nanometers)
        check_dimensionality(angle_position, unit.nanometers)
        check_dimensionality(torsion_position, unit.nanometers)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)

        # Compute Cartesian coordinates from internal coordinates using all-dimensionless quantities
        # All inputs to numba must be in float64 arrays implicitly in md_unit_syste units of nanometers and radians
        from perses.rjmc import coordinate_numba
        xyz = coordinate_numba.internal_to_cartesian(
            bond_position.value_in_unit(unit.nanometers).astype(np.float64),
            angle_position.value_in_unit(unit.nanometers).astype(np.float64),
            torsion_position.value_in_unit(unit.nanometers).astype(np.float64),
            np.array([r, theta, phi], np.float64))
        # Transform position of new atom back into unit-bearing Quantity
        xyz = unit.Quantity(xyz, unit=unit.nanometers)

        # Compute abs det Jacobian using unitless values
        detJ = np.abs(r**2*np.sin(theta))

        check_dimensionality(xyz, unit.nanometers)
        check_dimensionality(detJ, float)
        return xyz, detJ

    def _bond_log_pmf(self, bond, beta, n_divisions):
        """
        Calculate the log probability mass function (PMF) of drawing a bond.

        .. math ::

            p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        r_i : np.ndarray of shape (n_divisions,) implicitly in units of nanometers
            r_i[i] is the bond length leftmost bin edge with corresponding log probability mass function p_i[i]
        log_p_i : np.ndarray of shape (n_divisions,)
            log_p_i[i] is the corresponding log probability mass of bond length r_i[i]
        bin_width : float implicitly in units of nanometers
            The bin width for individual PMF bins


        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: We end up computing the discretized PMF over and over again; we can speed this up by caching
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        # Check input argument dimensions
        assert check_dimensionality(bond.type.req, unit.angstroms)
        assert check_dimensionality(bond.type.k, unit.kilojoules_per_mole/unit.nanometers**2)
        assert check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # Retrieve relevant quantities for valence bond
        r0 = bond.type.req # equilibrium bond distance, unit-bearing quantity
        k = bond.type.k * self._bond_softening_constant # force constant, unit-bearing quantity
        sigma_r = unit.sqrt((1.0/(beta*k))) # standard deviation, unit-bearing quantity

        # Convert to dimensionless quantities in MD unit system
        r0 = r0.value_in_unit_system(unit.md_unit_system)
        k = k.value_in_unit_system(unit.md_unit_system)
        sigma_r = sigma_r.value_in_unit_system(unit.md_unit_system)

        # Determine integration bounds
        lower_bound, upper_bound = max(0., r0 - 6*sigma_r), (r0 + 6*sigma_r)

        # Compute integration quadrature points
        r_i, bin_width = np.linspace(lower_bound, upper_bound, num=n_divisions, retstep=True, endpoint=False)

        # Form log probability
        from scipy.special import logsumexp
        log_p_i = 2*np.log(r_i+(bin_width/2.0)) - 0.5*((r_i+(bin_width/2.0)-r0)/sigma_r)**2
        log_p_i -= logsumexp(log_p_i)

        check_dimensionality(r_i, float)
        check_dimensionality(log_p_i, float)
        check_dimensionality(bin_width, float)

        return r_i, log_p_i, bin_width

    def _bond_logp(self, r, bond, beta, n_divisions):
        """
        Calculate the log-probability of a given bond at a given inverse temperature

        Propose dimensionless bond length r from distribution

        .. math ::

            r \sim p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        r : float
            bond length, implicitly in nanometers
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(r, float)
        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        r_i, log_p_i, bin_width = self._bond_log_pmf(bond, beta, n_divisions)

        if (r < r_i[0]) or (r >= r_i[-1] + bin_width):
            return LOG_ZERO

        # Determine index that r falls within
        index = int((r - r_i[0])/bin_width)
        assert (index >= 0) and (index < n_divisions)

        # Correct for division size
        logp = log_p_i[index] - np.log(bin_width)

        return logp

    def _propose_bond(self, bond, beta, n_divisions):
        """
        Propose dimensionless bond length r from distribution

        .. math ::

            r \sim p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        r : float
            Dimensionless bond length, implicitly in nanometers

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        r_i, log_p_i, bin_width = self._bond_log_pmf(bond, beta, n_divisions)

        # Draw an index
        index = np.random.choice(range(n_divisions), p=np.exp(log_p_i))
        r = r_i[index]

        # Draw uniformly in that bin
        r = np.random.uniform(r, r+bin_width)

        # Return dimensionless r, implicitly in nanometers
        assert check_dimensionality(r, float)
        assert (r > 0)
        return r

    def _angle_log_pmf(self, angle, beta, n_divisions):
        """
        Calculate the log probability mass function (PMF) of drawing a angle.

        .. math ::

            p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        theta_i : np.ndarray of shape (n_divisions,) implicitly in units of radians
            theta_i[i] is the angle with corresponding log probability mass function p_i[i]
        log_p_i : np.ndarray of shape (n_divisions,)
            log_p_i[i] is the corresponding log probability mass of angle theta_i[i]
        bin_width : float implicitly in units of radians
            The bin width for individual PMF bins

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        # TODO: We end up computing the discretized PMF over and over again; we can speed this up by caching

        # Check input argument dimensions
        assert check_dimensionality(angle.type.theteq, unit.radians)
        assert check_dimensionality(angle.type.k, unit.kilojoules_per_mole/unit.radians**2)
        assert check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # Retrieve relevant quantities for valence angle
        theta0 = angle.type.theteq
        k = angle.type.k * self._angle_softening_constant
        sigma_theta = unit.sqrt(1.0/(beta * k)) # standard deviation, unit-bearing quantity

        # Convert to dimensionless quantities in MD unit system
        theta0 = theta0.value_in_unit_system(unit.md_unit_system)
        k = k.value_in_unit_system(unit.md_unit_system)
        sigma_theta = sigma_theta.value_in_unit_system(unit.md_unit_system)

        # Determine integration bounds
        # We can't compute log(0) so we have to avoid sin(theta) = 0 near theta = {0, pi}
        EPSILON = 1.0e-3
        lower_bound, upper_bound = EPSILON, np.pi-EPSILON

        # Compute left bin edges
        theta_i, bin_width = np.linspace(lower_bound, upper_bound, num=n_divisions, retstep=True, endpoint=False)

        # Compute log probability
        from scipy.special import logsumexp
        log_p_i = np.log(np.sin(theta_i+(bin_width/2.0))) - 0.5*((theta_i+(bin_width/2.0)-theta0)/sigma_theta)**2
        log_p_i -= logsumexp(log_p_i)

        check_dimensionality(theta_i, float)
        check_dimensionality(log_p_i, float)
        check_dimensionality(bin_width, float)

        return theta_i, log_p_i, bin_width

    def _angle_logp(self, theta, angle, beta, n_divisions):
        """
        Calculate the log-probability of a given angle at a given inverse temperature

        Propose dimensionless bond length r from distribution

        .. math ::

            p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        theta : float
            angle, implicitly in radians
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence angle parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing angle

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(theta, float)
        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        theta_i, log_p_i, bin_width = self._angle_log_pmf(angle, beta, n_divisions)

        if (theta < theta_i[0]) or (theta >= theta_i[-1] + bin_width):
            return LOG_ZERO

        # Determine index that r falls within
        index = int((theta - theta_i[0]) / bin_width)
        assert (index >= 0) and (index < n_divisions)

        # Correct for division size
        logp = log_p_i[index] - np.log(bin_width)

        return logp

    def _propose_angle(self, angle, beta, n_divisions):
        """
        Propose dimensionless angle from distribution

        .. math ::

            \theta \sim p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence angle parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse temperature
        n_divisions : int
            Number of quandrature points for drawing angle

        Returns
        -------
        theta : float
            Dimensionless valence angle, implicitly in radians

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        theta_i, log_p_i, bin_width = self._angle_log_pmf(angle, beta, n_divisions)

        # Draw an index
        index = np.random.choice(range(n_divisions), p=np.exp(log_p_i))
        theta = theta_i[index]

        # Draw uniformly in that bin
        theta = np.random.uniform(theta, theta+bin_width)

        # Return dimensionless theta, implicitly in nanometers
        assert check_dimensionality(theta, float)
        return theta

    def _torsion_scan(self, torsion, positions, r, theta, n_divisions):
        """
        Compute unit-bearing Carteisan positions and torsions (dimensionless, in md_unit_system) for a torsion scan

        Parameters
        ----------
        torsion : parmed.Dihedral
            Parmed Dihedral containing relevant atoms defining torsion
        positions : simtk.unit.Quantity of shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in md_unit_system)
            Dimensionless bond length (must be in nanometers)
        theta : float (implicitly in md_unit_system)
            Dimensionless valence angle (must be in radians)
        n_divisions : int
            The number of divisions for the torsion scan

        Returns
        -------
        xyzs : simtk.unit.Quantity wrapped np.ndarray of shape (n_divisions,3) with dimensions length
            The cartesian coordinates of each
        phis : np.ndarray of shape (n_divisions,), implicitly in radians
            The torsions angles representing the left bin edge at which a potential will be calculated
        bin_width : float, implicitly in radians
            The bin width of torsion scan increment

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        assert check_dimensionality(positions, unit.angstroms)
        assert check_dimensionality(r, float)
        assert check_dimensionality(theta, float)

        # Compute dimensionless positions in md_unit_system as numba-friendly float64
        length_unit = unit.nanometers
        import copy
        positions_copy = copy.deepcopy(positions)
        positions_copy = positions_copy.value_in_unit(length_unit).astype(np.float64)
        bond_positions = positions_copy[torsion.atom2.idx]
        angle_positions = positions_copy[torsion.atom3.idx]
        torsion_positions = positions_copy[torsion.atom4.idx]

        # Compute dimensionless torsion values for torsion scan
        phis, bin_width = np.linspace(-np.pi, +np.pi, num=n_divisions, retstep=True, endpoint=False)

        # Compute dimensionless positions for torsion scan
        from perses.rjmc import coordinate_numba
        internal_coordinates = np.array([r, theta, 0.0], np.float64)
        xyzs = coordinate_numba.torsion_scan(bond_positions, angle_positions, torsion_positions, internal_coordinates, phis)

        # Convert positions back into standard md_unit_system length units (nanometers)
        xyzs_quantity = unit.Quantity(xyzs, unit=unit.nanometers)

        # Return unit-bearing positions and dimensionless torsions (implicitly in md_unit_system)
        check_dimensionality(xyzs_quantity, unit.nanometers)
        check_dimensionality(phis, float)
        return xyzs_quantity, phis, bin_width

    def _torsion_log_pmf(self, growth_context, torsion, positions, r, theta, beta, n_divisions):
        """
        Calculate the torsion log probability using OpenMM, including all energetic contributions for the atom being driven

        This includes all contributions from bonds, angles, and torsions for the atom being placed
        (and, optionally, sterics if added to the growth system when it was created).

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion : parmed.Dihedral modified to use simtk.unit.Quantity
            parmed Dihedral containing relevant atoms
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implcitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        logp_torsions : np.ndarray of float with shape (n_divisions,)
            logp_torsions[i] is the normalized probability density at phis[i]
        phis : np.ndarray of float with shape (n_divisions,), implicitly in radians
            phis[i] is the torsion angle left bin edges at which the log probability logp_torsions[i] was calculated
        bin_width : float implicitly in radian
            The bin width for torsions

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: This method could benefit from memoization to speed up tests and particle filtering
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute energies for all torsions
        logq = np.zeros(n_divisions) # logq[i] is the log unnormalized torsion probability density
        atom_idx = torsion.atom1.idx
        xyzs, phis, bin_width = self._torsion_scan(torsion, positions, r, theta, n_divisions)
        xyzs = xyzs.value_in_unit_system(unit.md_unit_system) # make positions dimensionless again
        positions = positions.value_in_unit_system(unit.md_unit_system)
        for i, xyz in enumerate(xyzs):
            # Set positions
            positions[atom_idx,:] = xyz
            growth_context.setPositions(positions)

            # Compute potential energy
            state = growth_context.getState(getEnergy=True)
            potential_energy = state.getPotentialEnergy()

            # Store unnormalized log probabilities
            logq_i = -beta*potential_energy
            logq[i] = logq_i

        # It's OK to have a few torsions with NaN energies,
        # but we need at least _some_ torsions to have finite energies
        if np.sum(np.isnan(logq)) == n_divisions:
            raise Exception("All %d torsion energies in torsion PMF are NaN." % n_divisions)

        # Suppress the contribution from any torsions with NaN energies
        logq[np.isnan(logq)] = -np.inf

        # Compute the normalized log probability
        from scipy.special import logsumexp
        logp_torsions = logq - logsumexp(logq)

        # Write proposed torsion energies to a PDB file for visualization or debugging, if desired
        if hasattr(self, '_proposal_pdbfile'):
            # Write proposal probabilities to PDB file as B-factors for inert atoms
            f_i = -logp_torsions
            f_i -= f_i.min() # minimum free energy is zero
            f_i[f_i > 999.99] = 999.99
            self._proposal_pdbfile.write('MODEL\n')
            for i, xyz in enumerate(xyzs):
                self._proposal_pdbfile.write('ATOM  %5d %4s %3s %c%4d    %8.3f%8.3f%8.3f%6.2f%6.2f\n' % (i+1, ' Ar ', 'Ar ', ' ', atom_idx+1, 10*xyz[0], 10*xyz[1], 10*xyz[2], np.exp(logp_torsions[i]), f_i[i]))
            self._proposal_pdbfile.write('TER\n')
            self._proposal_pdbfile.write('ENDMDL\n')
            # TODO: Write proposal PMFs to storage
            # atom_proposal_indices[order]
            # atom_positions[order,k]
            # torsion_pmf[order, division_index]

        assert check_dimensionality(logp_torsions, float)
        assert check_dimensionality(phis, float)
        assert check_dimensionality(bin_width, float)
        return logp_torsions, phis, bin_width

    def _propose_torsion(self, growth_context, torsion, positions, r, theta, beta, n_divisions):
        """
        Propose a torsion angle using OpenMM

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion : parmed.Dihedral modified to use simtk.unit.Quantity
            parmed Dihedral containing relevant atoms
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implcitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        phi : float, implicitly in radians
            The proposed torsion angle
        logp : float
            The log probability of the proposal

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute probability mass function for all possible proposed torsions
        logp_torsions, phis, bin_width = self._torsion_log_pmf(growth_context, torsion, positions, r, theta, beta, n_divisions)

        # Draw a torsion bin and a torsion uniformly within that bin
        index = np.random.choice(range(len(phis)), p=np.exp(logp_torsions))
        phi = phis[index]
        logp = logp_torsions[index]

        # Draw uniformly within the bin
        phi = np.random.uniform(phi, phi+bin_width)
        logp -= np.log(bin_width)

        assert check_dimensionality(phi, float)
        assert check_dimensionality(logp, float)
        return phi, logp

    def _torsion_logp(self, growth_context, torsion, positions, r, theta, phi, beta, n_divisions):
        """
        Calculate the logp of a torsion using OpenMM

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion : parmed.Dihedral modified to use simtk.unit.Quantity
            parmed Dihedral containing relevant atoms
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implicitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        phi : float (implicitly in radians on domain [-pi,+pi))
            Dimensionless torsion angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        torsion_logp : float
            The log probability this torsion would be drawn
        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities

        # Check that quantities are unitless
        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute torsion probability mass function
        logp_torsions, phis, bin_width = self._torsion_log_pmf(growth_context, torsion, positions, r, theta, beta, n_divisions)

        # Determine which bin the torsion falls within
        index = np.argmin(np.abs(phi-phis)) # WARNING: This assumes both phi and phis have domain of [-pi,+pi)

        # Convert from probability mass function to probability density function so that sum(dphi*p) = 1, with dphi = (2*pi)/n_divisions.
        torsion_logp = logp_torsions[index] - np.log(bin_width)

        assert check_dimensionality(torsion_logp, float)
        return torsion_logp

class GeometrySystemGenerator(object):
    """
    Internal utility class to generate OpenMM systems with only valence terms and special parameters for newly placed atoms to assist in geometry proposals.

    The resulting system will have the specified global context parameter (controlled by ``parameter_name``)
    that selects which proposed atom will have all its valence terms activated. When this parameter is set to the
    index of the atom being added within ``growth_indices``, all valence terms associated with that atom will be computed.
    Only valence terms involving newly placed atoms will be computed; valence terms between fixed atoms will be omitted.
    """

    def __init__(self, reference_system, growth_indices, parameter_name, add_extra_torsions=False, add_extra_angles=False, reference_topology=None, use_sterics=False, force_names=None, force_parameters=None, verbose=True):
        """
        Parameters
        ----------
        reference_system : simtk.openmm.System object
            The system containing the relevant forces and particles
        growth_indices : list of parmed.Atom
            List of parmed Atom objects defining the order in which the atom indices will be proposed
        global_parameter_name : str, optional, default='growth_index'
            The name of the global context parameter
        add_extra_torsions : bool, optional
            Whether to add additional torsions to keep rings flat. Default true.
        force_names : list of str
            A list of the names of forces that will be included in this system
        force_parameters : dict
            Options for the forces (e.g., NonbondedMethod : 'CutffNonPeriodic')
        verbose : bool, optional, default=False
            If True, will print verbose output
        reference_topology : simtk.openmm.app.Topology
            New (old) topology if forward (backward)

        .. todo ::

           Provide a mechanism for creating a NetworkX library of residue templates and biasing torsions/angles.
           We can pre-cache biopolymer residue templates for known biopolymer residues, and then generate new ones from OpenEye OEMols on the fly.
           Even better would be to use something like openforcefield Topology objects from which capped molecules can be generated and used to generate biases on the fly.

        """
        # TODO: Rename `growth_indices` (which is really a list of Atom objects) to `atom_growth_order` or `atom_addition_order`

        # Check that we're not using the reserved name
        if global_parameter_name == 'growth_idx':
            raise ValueError('global_parameter_name cannot be "growth_idx" due to naming collisions')

        default_growth_index = len(growth_indices) # default value of growth index to use in System that is returned
        self.current_growth_index = default_growth_index

        # Bonds, angles, and torsions
        self._HarmonicBondForceEnergy = "select(step({}+0.1 - growth_idx), (K/2)*(r-r0)^2, 0);"
        self._HarmonicAngleForceEnergy = "select(step({}+0.1 - growth_idx), (K/2)*(theta-theta0)^2, 0);"
        self._PeriodicTorsionForceEnergy = "select(step({}+0.1 - growth_idx), k*(1+cos(periodicity*theta-phase)), 0);"

        # Nonbonded sterics and electrostatics.
        # TODO: Allow user to select whether electrostatics or sterics components are included in the nonbonded interaction energy.
        self._nonbondedEnergy = "select(step({}+0.1 - growth_idx), U_sterics + U_electrostatics, 0);"
        self._nonbondedEnergy += "growth_idx = max(growth_idx1, growth_idx2);"
        # Sterics
        from openmmtools.constants import ONE_4PI_EPS0 # OpenMM constant for Coulomb interactions (implicitly in md_unit_system units)
        # TODO: Auto-detect combining rules to allow this to work with other force fields?
        # TODO: Enable more flexible handling / metaprogramming of CustomForce objects?
        self._nonbondedEnergy += "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/r)^6;"
        self._nonbondedEnergy += "epsilon = sqrt(epsilon1*epsilon2); sigma = 0.5*(sigma1 + sigma2);"
        # Electrostatics
        self._nonbondedEnergy += "U_electrostatics = ONE_4PI_EPS0*charge1*charge2/r;"
        self._nonbondedEnergy += "ONE_4PI_EPS0 = %f;" % ONE_4PI_EPS0

        # Exceptions (always included)
        self._nonbondedExceptionEnergy = "select(step({}+0.1 - growth_idx), U_exception, 0);"
        self._nonbondedExceptionEnergy += "U_exception = ONE_4PI_EPS0*chargeprod/r + 4*epsilon*x*(x-1.0); x = (sigma/r)^6;"
        self._nonbondedExceptionEnergy += "ONE_4PI_EPS0 = %f;" % ONE_4PI_EPS0

        self.sterics_cutoff_distance = 9.0 * unit.angstroms # cutoff for steric interactions with added/deleted atoms

        self.verbose = verbose

        # Get list of particle indices for new and old atoms.
        new_particle_indices = [ atom.idx for atom in growth_indices ] # atoms that will be added, one at a time
        old_particle_indices = [ idx for idx in range(reference_system.getNumParticles()) if idx not in new_particle_indices ] # fixed atoms

        # Compile index of reference forces
        reference_forces = dict()
        for (index, force) in enumerate(reference_system.getForces()):
            force_name = force.__class__.__name__
            if force_name in reference_forces:
                raise ValueError('reference_system has two {} objects. This is currently unsupported.'.format(force_name))
            else:
                reference_forces[force_name] = force

        # Create new System
        from simtk import openmm
        growth_system = openmm.System()

        # Copy particles
        for i in range(reference_system.getNumParticles()):
            growth_system.addParticle(reference_system.getParticleMass(i))

        # We don't need to copy constraints, since we will not be running dynamics with this system

        # Virtual sites are, in principle, automatically supported

        # Create bond force
        modified_bond_force = openmm.CustomBondForce(self._HarmonicBondForceEnergy.format(global_parameter_name))
        modified_bond_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['r0', 'K', 'growth_idx']:
            modified_bond_force.addPerBondParameter(parameter_name)
        growth_system.addForce(modified_bond_force)
        reference_bond_force = reference_forces['HarmonicBondForce']
        for bond_index in range(reference_bond_force.getNumBonds()):
            p1, p2, r0, K = reference_bond_force.getBondParameters(bond_index)
            growth_idx = self._calculate_growth_idx([p1, p2], growth_indices)
            if growth_idx > 0:
                modified_bond_force.addBond(p1, p2, [r0, K, growth_idx])

        # Create angle force
        modified_angle_force = openmm.CustomAngleForce(self._HarmonicAngleForceEnergy.format(global_parameter_name))
        modified_angle_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['theta0', 'K', 'growth_idx']:
            modified_angle_force.addPerAngleParameter(parameter_name)
        growth_system.addForce(modified_angle_force)
        reference_angle_force = reference_forces['HarmonicAngleForce']
        for angle in range(reference_angle_force.getNumAngles()):
            p1, p2, p3, theta0, K = reference_angle_force.getAngleParameters(angle)
            growth_idx = self._calculate_growth_idx([p1, p2, p3], growth_indices)
            if growth_idx > 0:
                modified_angle_force.addAngle(p1, p2, p3, [theta0, K, growth_idx])

        # Create torsion force
        modified_torsion_force = openmm.CustomTorsionForce(self._PeriodicTorsionForceEnergy.format(global_parameter_name))
        modified_torsion_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['periodicity', 'phase', 'k', 'growth_idx']:
            modified_torsion_force.addPerTorsionParameter(parameter_name)
        growth_system.addForce(modified_torsion_force)
        reference_torsion_force = reference_forces['PeriodicTorsionForce']
        for torsion in range(reference_torsion_force.getNumTorsions()):
            p1, p2, p3, p4, periodicity, phase, k = reference_torsion_force.getTorsionParameters(torsion)
            growth_idx = self._calculate_growth_idx([p1, p2, p3, p4], growth_indices)
            if growth_idx > 0:
                modified_torsion_force.addTorsion(p1, p2, p3, p4, [periodicity, phase, k, growth_idx])

        # Add (1,4) exceptions, regardless of whether 'use_sterics' is specified, because these are part of the valence forces.
        if 'NonbondedForce' in reference_forces.keys():
            custom_bond_force = openmm.CustomBondForce(self._nonbondedExceptionEnergy.format(global_parameter_name))
            custom_bond_force.addGlobalParameter(global_parameter_name, default_growth_index)
            for parameter_name in ['chargeprod', 'sigma', 'epsilon', 'growth_idx']:
                custom_bond_force.addPerBondParameter(parameter_name)
            growth_system.addForce(custom_bond_force)
            # Add exclusions, which are active at all times.
            # (1,4) exceptions are always included, since they are part of the valence terms.
            reference_nonbonded_force = reference_forces['NonbondedForce']
            for exception_index in range(reference_nonbonded_force.getNumExceptions()):
                p1, p2, chargeprod, sigma, epsilon = reference_nonbonded_force.getExceptionParameters(exception_index)
                growth_idx = self._calculate_growth_idx([p1, p2], growth_indices)
                # Only need to add terms that are nonzero and involve newly added atoms.
                if (growth_idx > 0) and ((chargeprod.value_in_unit_system(unit.md_unit_system) != 0.0) or (epsilon.value_in_unit_system(unit.md_unit_system) != 0.0)):
                    if self.verbose: _logger.info('Adding CustomBondForce: %5d %5d : chargeprod %8.3f e^2, sigma %8.3f A, epsilon %8.3f kcal/mol, growth_idx %5d' % (p1, p2, chargeprod/unit.elementary_charge**2, sigma/unit.angstrom, epsilon/unit.kilocalorie_per_mole, growth_idx))
                    custom_bond_force.addBond(p1, p2, [chargeprod, sigma, epsilon, growth_idx])

        # Copy parameters for local sterics parameters in nonbonded force
        if use_sterics and 'NonbondedForce' in reference_forces.keys():
            modified_sterics_force = openmm.CustomNonbondedForce(self._nonbondedEnergy.format(global_parameter_name))
            modified_sterics_force.addGlobalParameter(global_parameter_name, default_growth_index)
            for parameter_name in ['charge', 'sigma', 'epsilon', 'growth_idx']:
                modified_sterics_force.addPerParticleParameter(parameter_name)
            growth_system.addForce(modified_sterics_force)
            # Translate nonbonded method to cutoff methods.
            reference_nonbonded_force = reference_forces['NonbondedForce']
            if reference_nonbonded_force in [openmm.NonbondedForce.NoCutoff, openmm.NonbondedForce.CutoffNonPeriodic]:
                modified_sterics_force.setNonbondedMethod(openmm.CustomNonbondedForce.CutoffNonPeriodic)
            elif reference_nonbonded_force in [openmm.NonbondedForce.CutoffPeriodic, openmm.NonbondedForce.PME, openmm.NonbondedForce.Ewald]:
                modified_sterics_force.setNonbondedMethod(openmm.CustomNonbondedForce.CutoffPeriodic)
            modified_sterics_force.setCutoffDistance(self.sterics_cutoff_distance)
            # Add particle parameters.
            for particle_index in range(reference_nonbonded_force.getNumParticles()):
                [charge, sigma, epsilon] = reference_nonbonded_force.getParticleParameters(particle_index)
                growth_idx = self._calculate_growth_idx([particle_index], growth_indices)
                modified_sterics_force.addParticle([charge, sigma, epsilon, growth_idx])
                if self.verbose and (growth_idx > 0):
                    _logger.info('Adding NonbondedForce particle %5d : charge %8.3f |e|, sigma %8.3f A, epsilon %8.3f kcal/mol, growth_idx %5d' % (particle_index, charge/unit.elementary_charge, sigma/unit.angstrom, epsilon/unit.kilocalorie_per_mole, growth_idx))
            # Add exclusions, which are active at all times.
            # (1,4) exceptions are always included, since they are part of the valence terms.
            for exception_index in range(reference_nonbonded_force.getNumExceptions()):
                [p1, p2, chargeprod, sigma, epsilon] = reference_nonbonded_force.getExceptionParameters(exception_index)
                modified_sterics_force.addExclusion(particle_index_1, particle_index_2)
            # Only compute interactions of new particles with all other particles
            # TODO: Allow inteactions to be resticted to only the residue being grown.
            modified_sterics_force.addInteractionGroup(set(new_particle_indices), set(old_particle_indices))
            modified_sterics_force.addInteractionGroup(set(new_particle_indices), set(new_particle_indices))

        # Add extra ring-closing torsions, if requested.
        # NOTE: These will not work correctly with polymer residues (yet)
        if add_extra_torsions:
            if reference_topology==None:
                raise ValueError("Need to specify topology in order to add extra torsions.")
            self._determine_extra_torsions(modified_torsion_force, reference_topology, reference_oemol, growth_indices)
        if add_extra_angles:
            if reference_topology==None:
                raise ValueError("Need to specify topology in order to add extra angles")
            self._determine_extra_angles(modified_angle_force, reference_topology, reference_oemol, growth_indices)

        # Store growth system
        self._growth_parameter_name = global_parameter_name
        self._growth_system = growth_system

    def set_growth_parameter_index(self, growth_parameter_index, context=None):
        """
        Set the growth parameter index
        """
        # TODO: Set default force global parameters if context is not None.
        if context is not None:
            context.setParameter(self._growth_parameter_name, growth_parameter_index)
        self.current_growth_index = growth_parameter_index

    def get_modified_system(self):
        """
        Create a modified system with parameter_name parameter. When 0, only core atoms are interacting;
        for each integer above 0, an additional atom is made interacting, with order determined by growth_index.

        Returns
        -------
        growth_system : simtk.openmm.System object
            System with the appropriate modifications, with growth parameter set to maximum.
        """
        return self._growth_system

    def _determine_extra_torsions(self, torsion_force, reference_topology, growth_indices):
        """
        In order to facilitate ring closure and ensure proper bond stereochemistry,
        we add additional biasing torsions to rings and stereobonds that are then corrected
        for in the acceptance probability.

        Determine which residue is covered by the new atoms
        Identify rotatable bonds
        Construct analogous residue in OpenEye and generate configurations with Omega
        Measure appropriate torsions and generate relevant parameters

        .. warning :: Only one residue should be changing

        .. warning :: This currently will not work for polymer residues

        .. todo :: Use a database of biasing torsions constructed ahead of time and match to residues by NetworkX

        Parameters
        ----------
        torsion_force : openmm.CustomTorsionForce object
            the new/old torsion force if forward/backward
        reference_topology : openmm.app.Topology object
            the new/old topology if forward/backward
        oemol : openeye.oechem.OEMol
            An OEMol representing the new (old) system if forward (backward)
        growth_indices : list of int
            The list of new atoms and the order in which they will be added.

        Returns
        -------
        torsion_force : openmm.CustomTorsionForce
            The torsion force with extra torsions added appropriately.

        """
        from openeye import oechem, oeomega
        from simtk import openmm

        # Do nothing if there are no atoms to grow.
        if len(growth_indices) == 0:
            return torsion_force

        #get the list of torsions in the molecule that are not about a rotatable bond
        # Note that only torsions involving heavy atoms are enumerated here.
        rotor = oechem.OEIsRotor()
        torsion_predicate = oechem.OENotBond(rotor)
        non_rotor_torsions = list(oechem.OEGetTorsions(oemol, torsion_predicate))
        relevant_torsion_list = self._select_torsions_without_h(non_rotor_torsions)

        #now, for each torsion, extract the set of indices and the angle
        periodicity = 1
        k = 120.0*unit.kilocalories_per_mole # stddev of 12 degrees
        #print([atom.name for atom in growth_indices])
        for torsion in relevant_torsion_list:
            #make sure to get the atom index that corresponds to the topology
            atom_indices = [torsion.a.GetData("topology_index"), torsion.b.GetData("topology_index"), torsion.c.GetData("topology_index"), torsion.d.GetData("topology_index")]
            # Determine phase in [-pi,+pi) interval
            #phase = (np.pi)*unit.radians+angle
            phase = torsion.radians + np.pi # TODO: Check that this is the correct convention?
            while (phase >= np.pi):
                phase -= 2*np.pi
            while (phase < -np.pi):
                phase += 2*np.pi
            phase *= unit.radian
            #print('PHASE>>>> ' + str(phase)) # DEBUG
            growth_idx = self._calculate_growth_idx(atom_indices, growth_indices)
            atom_names = [torsion.a.GetName(), torsion.b.GetName(), torsion.c.GetName(), torsion.d.GetName()]
            #print("Adding torsion with atoms %s and growth index %d" %(str(atom_names), growth_idx))
            #If this is a CustomTorsionForce, we need to pass the parameters as a list, and it will have the growth_idx parameter.
            #If it's a regular PeriodicTorsionForce, there is no growth_index and the parameters are passed separately.
            if isinstance(torsion_force, openmm.CustomTorsionForce):
                torsion_force.addTorsion(atom_indices[0], atom_indices[1], atom_indices[2], atom_indices[3], [periodicity, phase, k, growth_idx])
            elif isinstance(torsion_force, openmm.PeriodicTorsionForce):
                torsion_force.addTorsion(atom_indices[0], atom_indices[1], atom_indices[2], atom_indices[3], periodicity, phase, k)
            else:
                raise ValueError("The force supplied to this method must be either a CustomTorsionForce or a PeriodicTorsionForce")

        return torsion_force

    def _select_torsions_without_h(self, torsion_list):
        """
        Return only torsions that do not contain hydrogen

        Parameters
        ----------
        torsion_list : list of oechem.OETorsion

        Returns
        -------
        heavy_torsions : list of oechem.OETorsion
        """
        heavy_torsions = []
        for torsion in torsion_list:
            is_h_present = torsion.a.IsHydrogen() + torsion.b.IsHydrogen() + torsion.c.IsHydrogen() + torsion.d.IsHydrogen()
            if not is_h_present:
                heavy_torsions.append(torsion)
        return heavy_torsions

    def _determine_extra_angles(self, angle_force, reference_topology, oemol, growth_indices):
        """
        Determine extra angles to be placed on aromatic ring members. Sometimes,
        the native angle force is too weak to efficiently close the ring. As with the
        torsion force, this method assumes that only one residue is changing at a time.

        .. todo :: Use a database of biasing torsions constructed ahead of time and match to residues by NetworkX

        .. warning :: This currently will not work for polymer residues

        Parameters
        ----------
        angle_force : simtk.openmm.CustomAngleForce
            the force to which additional terms will be added
        reference_topology : simtk.openmm.app.Topology
            new/old topology if forward/backward
        oemol : openeye.oechem.OEMol
            An OEMol representing the new (old) system if forward (backward)
        growth_indices : list of int
            The list of new atoms and the order in which they will be added.

        Returns
        -------
        angle_force : simtk.openmm.CustomAngleForce
            The modified angle force
        """
        from openeye import oechem, oeomega
        from simtk import openmm
        import itertools
        if len(growth_indices)==0:
            return
        angle_force_constant = 400.0*unit.kilojoules_per_mole/unit.radians**2
        atoms = list(reference_topology.atoms())

        #we now have the residue as an oemol. Time to find the relevant angles.
        #There's no equivalent to OEGetTorsions, so first find atoms that are relevant
        #TODO: find out if that's really true
        aromatic_pred = oechem.OEIsAromaticAtom()
        heavy_pred = oechem.OEIsHeavy()
        angle_criteria = oechem.OEAndAtom(aromatic_pred, heavy_pred)

        #get all heavy aromatic atoms:
        #TODO: do this more efficiently
        heavy_aromatics = list(oemol.GetAtoms(angle_criteria))
        for atom in heavy_aromatics:
            #bonded_atoms = [bonded_atom for bonded_atom in list(atom.GetAtoms()) if bonded_atom in heavy_aromatics]
            bonded_atoms = list(atom.GetAtoms())
            for angle_atoms in itertools.combinations(bonded_atoms, 2):
                    angle = oechem.OEGetAngle(oemol, angle_atoms[0], atom, angle_atoms[1])
                    atom_indices = [angle_atoms[0].GetData("topology_index"), atom.GetData("topology_index"), angle_atoms[1].GetData("topology_index")]
                    angle_radians = angle*unit.radian
                    growth_idx = self._calculate_growth_idx(atom_indices, growth_indices)
                    #If this is a CustomAngleForce, we need to pass the parameters as a list, and it will have the growth_idx parameter.
                    #If it's a regular HarmonicAngleForce, there is no growth_index and the parameters are passed separately.
                    if isinstance(angle_force, openmm.CustomAngleForce):
                        angle_force.addAngle(atom_indices[0], atom_indices[1], atom_indices[2], [angle_radians, angle_force_constant, growth_idx])
                    elif isinstance(angle_force, openmm.HarmonicAngleForce):
                        angle_force.addAngle(atom_indices[0], atom_indices[1], atom_indices[2], angle_radians, angle_force_constant)
                    else:
                        raise ValueError("Angle force must be either CustomAngleForce or HarmonicAngleForce")
        return angle_force

    def _calculate_growth_idx(self, particle_indices, growth_indices):
        """
        Utility function to calculate the growth index of a particular force.

        For each particle index, it will check to see if it is in growth_indices.
        If not, 0 is added to an array, if yes, the index in growth_indices is added.
        Finally, the method returns the max of the accumulated array

        Parameters
        ----------
        particle_indices : list of int
            The indices of particles involved in this force term (e.g. a bond, angle, or torsion)
        growth_indices : list of parmed.Atom
            The ordered list of parmed Atom objects defining the order in which atoms are to be added

        Returns
        -------
        growth_idx : int
            The growth index for the atom to be added
            0 denotes it is part of the fixed atoms
            1,2,3,... denote atoms sequentially added in that order
        """
        growth_indices_list = [ atom.idx for atom in list(growth_indices) ]
        particle_indices_set = set(particle_indices)
        growth_indices_set = set(growth_indices)
        new_atoms_in_force = particle_indices_set.intersection(growth_indices_set)

        if len(new_atoms_in_force) == 0:
            # This is a fixed atom
            return 0
        new_atom_growth_order = [growth_indices.index(atom_idx)+1 for atom_idx in new_atoms_in_force]
        return max(new_atom_growth_order)

class NetworkXProposalOrder(object):
    """
    This is a proposal order generating object that uses just networkx and graph traversal for simplicity.
    """

    def __init__(self, topology_proposal, direction="forward"):
        """
        Create a NetworkXProposalOrder class
        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            Container class for the transformation
        direction: str, default forward
            Whether to go forward or in reverse for the proposal.
        """
        self._topology_proposal = topology_proposal
        self._direction = direction

        self._hydrogen = app.Element.getByAtomicNumber(1.0)

        # Set the direction
        if direction == "forward":
            self._destination_system = self._topology_proposal.new_system
            self._new_atoms = self._topology_proposal.unique_new_atoms
            self._destination_topology = self._topology_proposal.new_topology
            self._atoms_with_positions = self._topology_proposal.new_to_old_atom_map.keys()
        elif direction == "reverse":
            self._destination_system = self._topology_proposal.old_system
            self._new_atoms = self._topology_proposal.unique_old_atoms
            self._destination_topology = self._topology_proposal.old_topology
            self._atoms_with_positions = self._topology_proposal.old_to_new_atom_map.keys()
        else:
            raise ValueError("Direction must be either forward or reverse.")

        self._new_atom_objects = list(self._destination_topology.atoms())

        self._atoms_with_positions_set = set(self._atoms_with_positions)

        self._hydrogens = []
        self._heavy = []

        # Sort the new atoms into hydrogen and heavy atoms:
        for atom in self._new_atom_objects:
            if atom.element == self._hydrogen:
                self._hydrogens.append(atom.index)
            else:
                self._heavy.append(atom.index)

        # Sanity check
        if len(self._hydrogens)==0 and len(self._heavy)==0:
            msg = 'NetworkXProposalOrder: No new atoms for direction {}\n'.format(direction)
            msg += str(topology_proposal)
            raise Exception(msg)

        # Choose the first of the new atoms to find the corresponding residue:
        transforming_residue = self._new_atom_objects[self._new_atoms[0]].residue

        self._residue_graph = self._residue_to_graph(transforming_residue)

    def determine_proposal_order(self):
        """
        Determine the proposal order of this system pair.
        This includes the choice of a torsion. As such, a logp is returned.

        Parameters
        ----------
        direction : str, optional
            whether to determine the forward or reverse proposal order

        Returns
        -------
        atom_torsions : list of list of int
            A list of torsions, where the first atom in the torsion is the one being proposed
        logp_torsion_choice : float
            log probability of the chosen torsions

        """
        heavy_atoms_torsions, heavy_logp = self._propose_atoms_in_order(self._heavy)
        hydrogen_atoms_torsions, hydrogen_logp = self._propose_atoms_in_order(self._hydrogens)
        proposal_order = heavy_atoms_torsions + hydrogen_atoms_torsions

        if proposal_order is None:
            msg = 'NetworkXProposalOrder: proposal_order is empty\n'
            raise Exception(msg)

        return proposal_order, heavy_logp + hydrogen_logp

    def _propose_atoms_in_order(self, atom_group):
        """
        Propose a group of atoms along with corresponding torsions and a total log probability for the choice
        Parameters
        ----------
        atom_group : list of int
            The atoms to propose
        Returns
        -------
        atom_torsions : list of list of int
            A list of torsions, where the first atom in the torsion is the one being proposed
        logp : float
            The contribution to the overall proposal log probability
        """
        import networkx as nx
        from scipy import special
        atom_torsions = []
        logp = 0.0
        while len(atom_group) > 0:
            proposal_atoms = {}

            for atom_idx in atom_group:
                # Find the shortest path up to length four from the atom in question:
                shortest_paths = nx.algorithms.single_source_shortest_path(self._residue_graph, atom_idx, cutoff=4)

                # Loop through shortest paths to find all paths of length 4 to an atom with positions:
                eligible_torsions_list = []

                for destination, path in shortest_paths.items():

                    # Check if the path is length 4 (a torsion) and that the destination has a position. Continue if not.
                    if len(path) != 4 or destination not in self._atoms_with_positions:
                        continue

                    # If the last atom is in atoms with positions, check to see if the others are also.
                    # If they are, append the torsion to the list of possible torsions to propose
                    if set(path[1:3]).issubset(self._atoms_with_positions_set):
                        eligible_torsions_list.append(path)

                # If there are any eligible torsions, choose one randomly, add it to the atom_torsions,
                # and mark this atom as having a position
                ntorsions = len(eligible_torsions_list)
                if len(eligible_torsions_list) > 0:
                    torsion_index = np.random.choice(range(ntorsions))
                    atom_torsion = eligible_torsions_list[torsion_index]
                    proposal_atoms[atom_idx] = atom_torsion

                    # Add the appropriate logP contribution for uniform choice:
                    logp += np.log(1/len(eligible_torsions_list))
                    self._atoms_with_positions_set.add(atom_idx)

            # Remove the newly added atoms from the atom group:
            for atom_idx in proposal_atoms:
                atom_group.remove(atom_idx)

            # Choose the order in which to add these atoms:
            atoms_to_order = list(proposal_atoms.keys())
            atom_order = np.random.choice(atoms_to_order, size=len(atoms_to_order), replace=False)

            # Add these atoms to the atom torsion list:
            for atom in atom_order:
                atom_torsions.append(proposal_atoms[atom])

            #Add to the logp:
            logp += -special.gammaln(len(atoms_to_order) + 1)

        return atom_torsions, logp

    def _residue_to_graph(self, residue):
        """
        Create a NetworkX graph representing the connectivity of a residue
        Parameters
        ----------
        residue : simtk.openmm.app.Residue
            The residue to use to create the graph
        Returns
        -------
        residue_graph : nx.Graph
            A graph representation of the residue
        """
        import networkx as nx
        g = nx.Graph()

        for atom in residue.atoms():
            g.add_node(atom)

        for bond in residue.bonds():
            g.add_edge(bond[0].index, bond[1].index)

        return g
