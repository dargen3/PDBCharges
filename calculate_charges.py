import argparse
from os import path, system
from time import time
import dimorphite_dl
import biotite.structure as biotite_structure
import biotite.structure.io.pdbx as biotite_mmCIF

import hydride
from Bio import PDB
from moleculekit import molecule as molit
from moleculekit.tools.preparation import systemPrepare
from openmm.app import PDBxFile
from pdbfixer import PDBFixer
import numpy as np
from math import dist
import gemmi
from collections import Counter
import json


def load_arguments():
    print("\nParsing arguments... ", end="")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mmCIF_file", type=str, required=True,
                        help="mmCIF file with protein structure.")
    parser.add_argument("--data_dir", type=str, required=True,
                        help="Directory for saving results.")
    parser.add_argument("--pH", type=float, required=False, default=7.2,
                        help="PDB file with protein structure.")
    args = parser.parse_args()
    if not 0 <= args.pH <= 14:
        exit(f"\nERROR! The pH value must be between 0 and 14!\n")
    if not path.isfile(args.mmCIF_file):
        exit(f"\nERROR! File {args.mmCIF_file} does not exist!\n")
    if path.exists(args.data_dir):
        exit(f"\nError! Directory with name {args.data_dir} exists. "
             f"Remove existed directory or change --data_dir argument!\n")
    print("ok")
    return args


class SelectAtoms(PDB.Select):
    def accept_atom(self, atom):
        if atom.full_id in self.full_ids:
            return 1
        else:
            return 0


class ChargesCalculator:
    def __init__(self,
                 mmCIF_file: str,
                 data_dir: str,
                 pH: float):
        self.data_dir = data_dir
        self.pH = pH
        system(f"mkdir {self.data_dir}; "
               f"mkdir {self.data_dir}/logs; "
               f"cp {mmCIF_file} {self.data_dir}")
        self.mmCIF_file = f"{self.data_dir}/{path.basename(mmCIF_file)}"
        resnames = [residue.resname for residue in PDB.MMCIFParser(QUIET=True).get_structure("structure", self.mmCIF_file)[0].get_residues()]
        with open(f"{self.data_dir}/logs/residues_info.txt", "w") as structure_info_file:
            for resname, count in Counter(resnames).most_common():
                structure_info_file.write(f"{resname} {count}\n")
            structure_info_file.write(f"Total {len(resnames)}\n")
        self.set_of_resnames = set(resnames)

        # We keep a biotite file in memory where we can change only what we want.
        # For example, pdbfixer sometimes deletes right values in the struct_conn mmCIF block.
        # So we just read the atom_site block from the fixed file and write it to the self.biotite_mmCIF_file
        self.biotite_mmCIF_file = biotite_mmCIF.CIFFile.read(self.mmCIF_file)


    def calculate_charges(self):
        self.fix_structure()
        self.remove_hydrogens()
        self.protonate_heteroresidues()

        propka_charges = self.protonate_protein()
        if all(chg == 0 for chg in propka_charges):
            exit("ERROR! Moleculekit is not modified!")
            # /home/dargen3/miniconda3/lib/python3.11/site-packages/moleculekit/tools/preparation.py
            #  line 827 ("ffcharge", "charge")
            # https://github.com/Acellera/moleculekit/issues/136
        structure = PDB.MMCIFParser(QUIET=True).get_structure("structure", f"{self.data_dir}/protonated_protein.cif")[0]

        # structure = PDB.PDBParser(QUIET=True).get_structure("structure", self.mmCIF_file)[0]
        # system(f"cd {self.data_dir} ; pdb2pqr --assign-only {path.basename(self.mmCIF_file)} smazat.pqr")
        # propka_charges = [float(line.split()[8]) for line in open(f"{self.data_dir}/smazat.pqr").readlines()[:-2]]


        propka_charges = np.nan_to_num(propka_charges)
        print("spch: " + str(sum(propka_charges)))

        for atom, propka_charge in zip(structure.get_atoms(), propka_charges):
            atom.propka_charge = propka_charge
            atom.cm5_charge = None
        kdtree = PDB.NeighborSearch(list(structure.get_atoms()))
        selector = SelectAtoms()
        io = PDB.PDBIO()
        io.set_structure(structure)
        radius = 6
        for i, atom in enumerate(structure.get_atoms()):
            if atom.element == "H":
                continue
            if atom.element == "O":
                if len(kdtree.search(atom.coord, 1.5, level="A")) <= 2:
                    continue

            t = time()
            substructure_data_dir = f"{self.data_dir}/sub_{i}"
            system(f"mkdir {substructure_data_dir}")

            atoms_up_to_radius = kdtree.search(atom.coord, radius, level="A")
            selector.full_ids = set([atom.full_id for atom in atoms_up_to_radius])
            io.save(f"{substructure_data_dir}/atoms_up_to_{radius}_angstroms.pdb", selector)

            atoms_up_to_12A = kdtree.search(atom.coord, 12, level="A")
            selector.full_ids = set([atom.full_id for atom in atoms_up_to_12A])
            io.save(f"{substructure_data_dir}/atoms_up_to_12_angstroms.pdb", selector)

            calculated_atoms = kdtree.search(atom.coord, 1.5, level="A") # ať se počítají jen centrální atom, dvouvazné kyslíky a vodíky
            calculated_atoms_full_ids = set([calculated_atom.full_id[1:] for calculated_atom in calculated_atoms])

            from rdkit import Chem
            mol_6A = Chem.MolFromPDBFile(f"{substructure_data_dir}/atoms_up_to_{radius}_angstroms.pdb", removeHs=False, sanitize=False)
            mol_6A_conformer = mol_6A.GetConformer()
            mol_12A = Chem.MolFromPDBFile(f"{substructure_data_dir}/atoms_up_to_12_angstroms.pdb", removeHs=False, sanitize=False)
            mol_12A_conformer = mol_12A.GetConformer()


            mol_6A_coord_dict = {}
            for aatom in mol_6A.GetAtoms():
                position = mol_6A_conformer.GetAtomPosition(aatom.GetIdx())
                mol_6A_coord_dict[(position.x, position.y, position.z)] = aatom
            mol_12A_coord_dict = {}
            for aatom in mol_12A.GetAtoms():
                position = mol_12A_conformer.GetAtomPosition(aatom.GetIdx())
                mol_12A_coord_dict[(position.x, position.y, position.z)] = aatom

            atoms_with_broken_bonds = []
            for aatom in mol_6A.GetAtoms():
                position = mol_6A_conformer.GetAtomPosition(aatom.GetIdx())
                mol_12A_atom = mol_12A_coord_dict[(position.x, position.y, position.z)]
                if len(aatom.GetNeighbors()) != len(mol_12A_atom.GetNeighbors()):
                    atoms_with_broken_bonds.append(mol_12A_atom)

            carbons_with_broken_bonds_positions = []
            while atoms_with_broken_bonds:
                atom_with_broken_bonds = atoms_with_broken_bonds.pop(0)
                bonded_atoms = atom_with_broken_bonds.GetNeighbors()
                for ba in bonded_atoms:
                    position = mol_12A_conformer.GetAtomPosition(ba.GetIdx())
                    if (position.x, position.y, position.z) in mol_6A_coord_dict:
                        continue
                    else:
                        if atom_with_broken_bonds.GetSymbol() == "C" and ba.GetSymbol() == "C":
                            carbons_with_broken_bonds_positions.append(mol_12A_conformer.GetAtomPosition(atom_with_broken_bonds.GetIdx()))
                            continue
                        else:
                            atoms_with_broken_bonds.append(ba)
                            mol_6A_coord_dict[(position.x, position.y, position.z)] = ba


            substructure_atoms = []
            for atom in atoms_up_to_12A:
                if tuple(round(float(x),3) for x in atom.coord) in mol_6A_coord_dict:
                    substructure_atoms.append(atom)


            selector.full_ids = set([atom.full_id for atom in substructure_atoms])
            io.save(f"{substructure_data_dir}/substructure.pdb", selector)

            substructure_charge = round(sum([aa.propka_charge for aa in substructure_atoms]))

            system(
             f"cd {substructure_data_dir} ; obabel -iPDB -oPDB substructure.pdb -h > reprotonated_substructure.pdb 2>/dev/null")
            with open(f"{substructure_data_dir}/reprotonated_substructure.pdb") as reprotonated_substructure_file:
                atom_lines = [line for line in reprotonated_substructure_file.readlines() if line[:4] in ["ATOM", "HETA"]]
                original_atoms = atom_lines[:len(substructure_atoms)]
                added_atoms = atom_lines[len(substructure_atoms):]


            with open(f"{substructure_data_dir}/repaired_substructure.pdb", "w") as repaired_substructure_file:
                repaired_substructure_file.write("".join(original_atoms))
                for added_atom in added_atoms:

                    if any([dist([float(added_atom[30:38]), float(added_atom[38:46]), float(added_atom[46:54])], (p.x, p.y, p.z)) < 1.3 for p in carbons_with_broken_bonds_positions]):
                        repaired_substructure_file.write(added_atom)




            system(f"cd {substructure_data_dir} ; "
                   f"xtb repaired_substructure.pdb --gfn 1 --gbsa water --acc 1000 --chrg {substructure_charge}   > xtb_output.txt 2> xtb_error_output.txt ")
            xtb_output_file_lines = open(f"{substructure_data_dir}/xtb_output.txt").readlines()
            charge_headline_index = xtb_output_file_lines.index(
                "  Mulliken/CM5 charges         n(s)   n(p)   n(d)\n")
            for substructure_index, substructure_atom in enumerate(PDB.PDBParser().get_structure("substructure",
                                                                                 f"{substructure_data_dir}/substructure.pdb").get_atoms()):
                if substructure_atom.full_id[1:] in calculated_atoms_full_ids: # todo, nejspíše přepisujeme přesněji vypočítané náboje za horši!
                    charge = float(xtb_output_file_lines[charge_headline_index + substructure_index + 1].split()[3])
                    structure[substructure_atom.full_id[2]][substructure_atom.get_parent().id][substructure_atom.id].cm5_charge = charge

            print(i, time() - t)
            exit()


        charges = np.array([atom.cm5_charge for atom in structure.get_atoms()])
        open(f"{self.data_dir}/charges.txt", "w").write(" ".join([str(x) for x in charges]))


        input_file = f"{self.data_dir}/protonated_protein.cif"
        structure = gemmi.cif.read_file(input_file)
        block = structure.sole_block()
        block.find_mmcif_category('_chem_comp.').erase() # remove pesky _chem_comp category >:(
        sb_ncbr_partial_atomic_charges_meta_prefix = "_sb_ncbr_partial_atomic_charges_meta."
        sb_ncbr_partial_atomic_charges_meta_attributes = ["id",
                                                  "type",
                                                  "method"]
        metadata_loop = block.init_loop(sb_ncbr_partial_atomic_charges_meta_prefix,
                                        sb_ncbr_partial_atomic_charges_meta_attributes)
        metadata_loop.add_row(['1',
                               "'empirical'",
                               "'SQE+qp/Schindler 2021 (PUB_pept)'"])
        sb_ncbr_partial_atomic_charges_prefix = "_sb_ncbr_partial_atomic_charges."
        sb_ncbr_partial_atomic_charges_attributes = ["type_id",
                                             "atom_id",
                                             "charge"]
        charges_loop = block.init_loop(sb_ncbr_partial_atomic_charges_prefix,
                                       sb_ncbr_partial_atomic_charges_attributes)
        for atomId, charge in enumerate(charges):
            charges_loop.add_row(["1",
                                  f"{atomId + 1}",
                                  f"{charge: .4f}"])
        block.write_file(f"{self.data_dir}/final.cif")





    def fix_structure(self):
        """
        mmCIF file is fixed by tool PDBFixer.
        https://github.com/openmm/pdbfixer

        PDBFixer solves common problems in protein structure files.
        It selects the first model, alternative locations, fills in missing heavy atoms, etc.

        PDBFixer removes values from a struct_conn block
        and therefore only atom_site block is overwritten from PDBFixer to self.biotite_mmCIF_file.
        """

        print("Fixing structure by PDBFixer... ", end="")
        fixer = PDBFixer(filename=self.mmCIF_file)

        # download templates for heteroresidues
        with open(f"{self.data_dir}/logs/pdbfixer_downloading_templates.txt", "w") as pdbfixer_log:
            fixer_available_resnames = set(fixer.templates.keys())
            for resname in self.set_of_resnames:
                if resname not in fixer_available_resnames:
                    try:
                        fixer.downloadTemplate(resname)
                        pdbfixer_log.write(f"Template for {resname} downloaded successfully.\n")
                    except:
                        pdbfixer_log.write(f"ERROR! Old version of pdbfixer installed or heteroresiduum {resname} does not exist!")
                        exit(f"ERROR! Old version of pdbfixer installed or heteroresiduum {resname} does not exist!")

        # add heavy atoms
        fixer.missingResidues = {}
        fixer.findMissingAtoms()
        with open(f"{self.data_dir}/logs/added_heavy_atoms.txt", "w") as added_heavy_atoms_file: # log it
            for residue, residue_list in fixer.missingAtoms.items():
                for atom in residue_list:
                    added_heavy_atoms_file.write(f"{residue} {atom}\n")
        fixer.addMissingAtoms()

        # write fixed structure to file
        PDBxFile.writeFile(fixer.topology, fixer.positions, open(f"{self.data_dir}/fixed.cif", 'w'), keepIds=True)

        # write only atom_site block to self.biotite_mmCIF_file
        fixed_mmCIF_file = biotite_mmCIF.CIFFile.read(f"{self.data_dir}/fixed.cif")
        self.biotite_mmCIF_file.block["atom_site"] = fixed_mmCIF_file.block["atom_site"]
        self.biotite_mmCIF_file.write(f"{self.data_dir}/fixed.cif")
        # these three lines can be probably removed, after biotite 1.0.2 will be released
        mmcif_string = open(f"{self.data_dir}/fixed.cif").read()
        repaired_mmcif_string = mmcif_string.replace("\n# ", "\n# \n")
        open(f"{self.data_dir}/fixed.cif", "w").write(repaired_mmcif_string)
        print("ok")

    def remove_hydrogens(self):
        biotite_protein = biotite_mmCIF.get_structure(self.biotite_mmCIF_file, model=1,extra_fields=["b_factor", "occupancy"])
        biotite_protein_without_hydrogens = biotite_protein[biotite_protein.element != "H"]
        biotite_mmCIF_file_without_hydrogens = biotite_mmCIF.CIFFile()
        biotite_mmCIF.set_structure(biotite_mmCIF_file_without_hydrogens, biotite_protein_without_hydrogens)
        self.biotite_mmCIF_file.block["atom_site"] = biotite_mmCIF_file_without_hydrogens.block["atom_site"]
        self.biotite_mmCIF_file.write(f"{self.data_dir}/without_hydrogens.cif")
        # these three lines can be probably removed, after biotite 1.0.2 will be released
        mmcif_string = open(f"{self.data_dir}/without_hydrogens.cif").read()
        repaired_mmcif_string = mmcif_string.replace("\n# ", "\n# \n")
        open(f"{self.data_dir}/without_hydrogens.cif", "w").write(repaired_mmcif_string)


    def protonate_heteroresidues(self):
        """
        This function is based on the biotite, hydride, RDKit and dimorphite_dl libraries.
        https://github.com/biotite-dev/biotite
        https://github.com/biotite-dev/hydride
        https://github.com/rdkit/rdkit
        https://github.com/durrantlab/dimorphite_dl

        The heteroresidues are protonated by the hydride library, which is built on top of the biotite library.
        Prior to the actual protonation, the formal charges are determined using the dimorphite_dl and RDKit libraries.
        Usually, from the mmCIF file, the ligand without hydrogens cannot be correctly constructed
        because of the order of bonding between the individual atoms.
        Therefore, the structure from the CCD dictionary is used as template.
        """

        print("Adding hydrogens to heteroresidues... ", end="")
        # pdb2pqr is part of moleculekit
        residues_protonated_by_pdb2pqr = set(['004', '03Y', '0A1', '0AF', '0BN', '1MH', '2AS', '2GX', '2ML', '2MR', '4IN', '4PH', '4PQ', '5JP', 'AA4', 'ABA', 'AHP', 'ALA', 'ALC', 'ALN', 'ALY', 'APD', 'ARG', 'ASN', 'ASP', 'BB8', 'BCS', 'BTK', 'CCS', 'CGU', 'CSA', 'CSO', 'CSP', 'CSS', 'CYS', 'D4P', 'DA2', 'DAB', 'DAH', 'DPP', 'ESC', 'FGL', 'GHG', 'GLN', 'GLU', 'GLY', 'GME', 'GNC', 'HHK', 'HIS', 'HLU', 'HLX', 'HOX', 'HPE', 'HQA', 'HTR', 'HYP', 'I2M', 'IGL', 'IIL', 'ILE', 'IML', 'KYN', 'LEU', 'LME', 'LMQ', 'LYS', 'LYZ', 'M3L', 'ME0', 'MEA', 'MEN', 'MEQ', 'MET', 'MLE', 'MLY', 'MLZ', 'MME', 'MMO', 'MVA', 'NAL', 'NCY', 'NLE', 'NVA', 'NZC', 'OCY', 'OMX', 'ONL', 'ORM', 'P1L', 'PCA', 'PHE', 'PRK', 'PRO', 'PTR', 'SEP', 'SER', 'THR', 'TPO', 'TRO', 'TRP', 'TY2', 'TYQ', 'TYR', 'VAL', 'WAT', 'YCM', 'YNM', 'RA', 'RC', 'RG', 'DT', 'RU', 'ASH', 'CYM', 'CYX', 'GLH', 'HSE', 'HSD', 'HSP', 'HID', 'HIE', 'HIP', 'AR0', 'LYN', 'TYM', 'C004', 'C03Y', 'C0A1', 'C0AF', 'C0BN', 'C1MH', 'C2AS', 'C2GX', 'C2ML', 'C2MR', 'C4IN', 'C4PH', 'C4PQ', 'C5JP', 'CAA4', 'CABA', 'CAHP', 'CALA', 'CALC', 'CALN', 'CALY', 'CAPD', 'CARG', 'CASN', 'CASP', 'CBB8', 'CBCS', 'CBTK', 'CCCS', 'CCGU', 'CCSA', 'CCSO', 'CCSP', 'CCSS', 'CCYS', 'CD4P', 'CDA2', 'CDAB', 'CDAH', 'CDPP', 'CESC', 'CFGL', 'CGHG', 'CGLN', 'CGLU', 'CGLY', 'CGME', 'CGNC', 'CHHK', 'CHIS', 'CHLU', 'CHLX', 'CHOX', 'CHPE', 'CHQA', 'CHTR', 'CHYP', 'CI2M', 'CIGL', 'CIIL', 'CILE', 'CIML', 'CKYN', 'CLEU', 'CLME', 'CLMQ', 'CLYS', 'CLYZ', 'CM3L', 'CME0', 'CMEA', 'CMEN', 'CMEQ', 'CMET', 'CMLE', 'CMLY', 'CMLZ', 'CMME', 'CMMO', 'CMVA', 'CNAL', 'CNCY', 'CNLE', 'CNVA', 'CNZC', 'COCY', 'COMX', 'CONL', 'CORM', 'CP1L', 'CPCA', 'CPHE', 'CPRK', 'CPRO', 'CPTR', 'CSEP', 'CSER', 'CTHR', 'CTPO', 'CTRO', 'CTRP', 'CTY2', 'CTYQ', 'CTYR', 'CVAL', 'CWAT', 'CYCM', 'CYNM', 'CASH', 'CCYM', 'CCYX', 'CGLH', 'CHSE', 'CHSD', 'CHSP', 'CHID', 'CHIE', 'CHIP', 'CAR0', 'CLYN', 'CTYM', 'NEUTRAL-C004', 'NEUTRAL-C03Y', 'NEUTRAL-C0A1', 'NEUTRAL-C0AF', 'NEUTRAL-C0BN', 'NEUTRAL-C1MH', 'NEUTRAL-C2AS', 'NEUTRAL-C2GX', 'NEUTRAL-C2ML', 'NEUTRAL-C2MR', 'NEUTRAL-C4IN', 'NEUTRAL-C4PH', 'NEUTRAL-C4PQ', 'NEUTRAL-C5JP', 'NEUTRAL-CAA4', 'NEUTRAL-CABA', 'NEUTRAL-CAHP', 'NEUTRAL-CALA', 'NEUTRAL-CALC', 'NEUTRAL-CALN', 'NEUTRAL-CALY', 'NEUTRAL-CAPD', 'NEUTRAL-CARG', 'NEUTRAL-CASN', 'NEUTRAL-CASP', 'NEUTRAL-CBB8', 'NEUTRAL-CBCS', 'NEUTRAL-CBTK', 'NEUTRAL-CCCS', 'NEUTRAL-CCGU', 'NEUTRAL-CCSA', 'NEUTRAL-CCSO', 'NEUTRAL-CCSP', 'NEUTRAL-CCSS', 'NEUTRAL-CCYS', 'NEUTRAL-CD4P', 'NEUTRAL-CDA2', 'NEUTRAL-CDAB', 'NEUTRAL-CDAH', 'NEUTRAL-CDPP', 'NEUTRAL-CESC', 'NEUTRAL-CFGL', 'NEUTRAL-CGHG', 'NEUTRAL-CGLN', 'NEUTRAL-CGLU', 'NEUTRAL-CGLY', 'NEUTRAL-CGME', 'NEUTRAL-CGNC', 'NEUTRAL-CHHK', 'NEUTRAL-CHIS', 'NEUTRAL-CHLU', 'NEUTRAL-CHLX', 'NEUTRAL-CHOX', 'NEUTRAL-CHPE', 'NEUTRAL-CHQA', 'NEUTRAL-CHTR', 'NEUTRAL-CHYP', 'NEUTRAL-CI2M', 'NEUTRAL-CIGL', 'NEUTRAL-CIIL', 'NEUTRAL-CILE', 'NEUTRAL-CIML', 'NEUTRAL-CKYN', 'NEUTRAL-CLEU', 'NEUTRAL-CLME', 'NEUTRAL-CLMQ', 'NEUTRAL-CLYS', 'NEUTRAL-CLYZ', 'NEUTRAL-CM3L', 'NEUTRAL-CME0', 'NEUTRAL-CMEA', 'NEUTRAL-CMEN', 'NEUTRAL-CMEQ', 'NEUTRAL-CMET', 'NEUTRAL-CMLE', 'NEUTRAL-CMLY', 'NEUTRAL-CMLZ', 'NEUTRAL-CMME', 'NEUTRAL-CMMO', 'NEUTRAL-CMVA', 'NEUTRAL-CNAL', 'NEUTRAL-CNCY', 'NEUTRAL-CNLE', 'NEUTRAL-CNVA', 'NEUTRAL-CNZC', 'NEUTRAL-COCY', 'NEUTRAL-COMX', 'NEUTRAL-CONL', 'NEUTRAL-CORM', 'NEUTRAL-CP1L', 'NEUTRAL-CPCA', 'NEUTRAL-CPHE', 'NEUTRAL-CPRK', 'NEUTRAL-CPRO', 'NEUTRAL-CPTR', 'NEUTRAL-CSEP', 'NEUTRAL-CSER', 'NEUTRAL-CTHR', 'NEUTRAL-CTPO', 'NEUTRAL-CTRO', 'NEUTRAL-CTRP', 'NEUTRAL-CTY2', 'NEUTRAL-CTYQ', 'NEUTRAL-CTYR', 'NEUTRAL-CVAL', 'NEUTRAL-CWAT', 'NEUTRAL-CYCM', 'NEUTRAL-CYNM', 'NEUTRAL-CASH', 'NEUTRAL-CCYM', 'NEUTRAL-CCYX', 'NEUTRAL-CGLH', 'NEUTRAL-CHSE', 'NEUTRAL-CHSD', 'NEUTRAL-CHSP', 'NEUTRAL-CHID', 'NEUTRAL-CHIE', 'NEUTRAL-CHIP', 'NEUTRAL-CAR0', 'NEUTRAL-CLYN', 'NEUTRAL-CTYM', 'N004', 'N03Y', 'N0A1', 'N0AF', 'N0BN', 'N1MH', 'N2AS', 'N2GX', 'N2ML', 'N2MR', 'N4IN', 'N4PH', 'N4PQ', 'N5JP', 'NAA4', 'NABA', 'NAHP', 'NALA', 'NALC', 'NALN', 'NALY', 'NAPD', 'NARG', 'NASN', 'NASP', 'NBB8', 'NBCS', 'NBTK', 'NCCS', 'NCGU', 'NCSA', 'NCSO', 'NCSP', 'NCSS', 'NCYS', 'ND4P', 'NDA2', 'NDAB', 'NDAH', 'NDPP', 'NESC', 'NFGL', 'NGHG', 'NGLN', 'NGLU', 'NGLY', 'NGME', 'NGNC', 'NHHK', 'NHIS', 'NHLU', 'NHLX', 'NHOX', 'NHPE', 'NHQA', 'NHTR', 'NHYP', 'NI2M', 'NIGL', 'NIIL', 'NILE', 'NIML', 'NKYN', 'NLEU', 'NLME', 'NLMQ', 'NLYS', 'NLYZ', 'NM3L', 'NME0', 'NMEA', 'NMEN', 'NMEQ', 'NMET', 'NMLE', 'NMLY', 'NMLZ', 'NMME', 'NMMO', 'NMVA', 'NNAL', 'NNCY', 'NNLE', 'NNVA', 'NNZC', 'NOCY', 'NOMX', 'NONL', 'NORM', 'NP1L', 'NPCA', 'NPHE', 'NPRK', 'NPRO', 'NPTR', 'NSEP', 'NSER', 'NTHR', 'NTPO', 'NTRO', 'NTRP', 'NTY2', 'NTYQ', 'NTYR', 'NVAL', 'NWAT', 'NYCM', 'NYNM', 'NASH', 'NCYM', 'NCYX', 'NGLH', 'NHSE', 'NHSD', 'NHSP', 'NHID', 'NHIE', 'NHIP', 'NAR0', 'NLYN', 'NTYM', 'NEUTRAL-N004', 'NEUTRAL-N03Y', 'NEUTRAL-N0A1', 'NEUTRAL-N0AF', 'NEUTRAL-N0BN', 'NEUTRAL-N1MH', 'NEUTRAL-N2AS', 'NEUTRAL-N2GX', 'NEUTRAL-N2ML', 'NEUTRAL-N2MR', 'NEUTRAL-N4IN', 'NEUTRAL-N4PH', 'NEUTRAL-N4PQ', 'NEUTRAL-N5JP', 'NEUTRAL-NAA4', 'NEUTRAL-NABA', 'NEUTRAL-NAHP', 'NEUTRAL-NALA', 'NEUTRAL-NALC', 'NEUTRAL-NALN', 'NEUTRAL-NALY', 'NEUTRAL-NAPD', 'NEUTRAL-NARG', 'NEUTRAL-NASN', 'NEUTRAL-NASP', 'NEUTRAL-NBB8', 'NEUTRAL-NBCS', 'NEUTRAL-NBTK', 'NEUTRAL-NCCS', 'NEUTRAL-NCGU', 'NEUTRAL-NCSA', 'NEUTRAL-NCSO', 'NEUTRAL-NCSP', 'NEUTRAL-NCSS', 'NEUTRAL-NCYS', 'NEUTRAL-ND4P', 'NEUTRAL-NDA2', 'NEUTRAL-NDAB', 'NEUTRAL-NDAH', 'NEUTRAL-NDPP', 'NEUTRAL-NESC', 'NEUTRAL-NFGL', 'NEUTRAL-NGHG', 'NEUTRAL-NGLN', 'NEUTRAL-NGLU', 'NEUTRAL-NGLY', 'NEUTRAL-NGME', 'NEUTRAL-NGNC', 'NEUTRAL-NHHK', 'NEUTRAL-NHIS', 'NEUTRAL-NHLU', 'NEUTRAL-NHLX', 'NEUTRAL-NHOX', 'NEUTRAL-NHPE', 'NEUTRAL-NHQA', 'NEUTRAL-NHTR', 'NEUTRAL-NHYP', 'NEUTRAL-NI2M', 'NEUTRAL-NIGL', 'NEUTRAL-NIIL', 'NEUTRAL-NILE', 'NEUTRAL-NIML', 'NEUTRAL-NKYN', 'NEUTRAL-NLEU', 'NEUTRAL-NLME', 'NEUTRAL-NLMQ', 'NEUTRAL-NLYS', 'NEUTRAL-NLYZ', 'NEUTRAL-NM3L', 'NEUTRAL-NME0', 'NEUTRAL-NMEA', 'NEUTRAL-NMEN', 'NEUTRAL-NMEQ', 'NEUTRAL-NMET', 'NEUTRAL-NMLE', 'NEUTRAL-NMLY', 'NEUTRAL-NMLZ', 'NEUTRAL-NMME', 'NEUTRAL-NMMO', 'NEUTRAL-NMVA', 'NEUTRAL-NNAL', 'NEUTRAL-NNCY', 'NEUTRAL-NNLE', 'NEUTRAL-NNVA', 'NEUTRAL-NNZC', 'NEUTRAL-NOCY', 'NEUTRAL-NOMX', 'NEUTRAL-NONL', 'NEUTRAL-NORM', 'NEUTRAL-NP1L', 'NEUTRAL-NPCA', 'NEUTRAL-NPHE', 'NEUTRAL-NPRK', 'NEUTRAL-NPRO', 'NEUTRAL-NPTR', 'NEUTRAL-NSEP', 'NEUTRAL-NSER', 'NEUTRAL-NTHR', 'NEUTRAL-NTPO', 'NEUTRAL-NTRO', 'NEUTRAL-NTRP', 'NEUTRAL-NTY2', 'NEUTRAL-NTYQ', 'NEUTRAL-NTYR', 'NEUTRAL-NVAL', 'NEUTRAL-NWAT', 'NEUTRAL-NYCM', 'NEUTRAL-NYNM', 'NEUTRAL-NASH', 'NEUTRAL-NCYM', 'NEUTRAL-NCYX', 'NEUTRAL-NGLH', 'NEUTRAL-NHSE', 'NEUTRAL-NHSD', 'NEUTRAL-NHSP', 'NEUTRAL-NHID', 'NEUTRAL-NHIE', 'NEUTRAL-NHIP', 'NEUTRAL-NAR0', 'NEUTRAL-NLYN', 'NEUTRAL-NTYM', 'HOH', 'DA', 'DA3', 'DA5', 'RA3', 'RA5', 'DC', 'DC3', 'DC5', 'RC3', 'RC5', 'DG', 'DG3', 'DG5', 'RG3', 'RG5', 'DT3', 'RU3', 'RU5'])
        # shortcuts for RNA, also protonated by pdb2pqr, defined in RNA_MAPPING
        residues_protonated_by_pdb2pqr.update(["A", "C", "G", "U"])

        structure = PDB.MMCIFParser(QUIET=True).get_structure("structure", f"{self.data_dir}/without_hydrogens.cif")[0]
        residues_protonated_by_dimorphite_dl = []
        for res in structure.get_residues():
            if not res.resname in residues_protonated_by_pdb2pqr:
                residues_protonated_by_dimorphite_dl.append(res)

        selector = SelectAtoms()
        io = PDB.PDBIO()
        io.set_structure(structure)
        for i, res in enumerate(residues_protonated_by_dimorphite_dl, start=1):

            selector.full_ids = set([atom.full_id for atom in res])
            io.save(f"{self.data_dir}/ligand_{i}.pdb", selector)


            from rdkit import Chem
            from dimorphite_dl import DimorphiteDL

            dimorphite_dl = DimorphiteDL(min_ph=7,
                             max_ph=7,
                             max_variants=1,
                             label_states=False,
                             pka_precision=0.01)

            CCD_mol = Chem.SDMolSupplier(f"IBP_ideal.sdf",
                          removeHs=True)[0]
            mol_smiles = Chem.MolToSmiles(CCD_mol)
            indices_to_original_mol = [int(x) for x in CCD_mol.GetProp('_smilesAtomOutputOrder')[1:-1].split(",")[:-1]]
            charged_smiles = dimorphite_dl.protonate(mol_smiles)[0]
            charged_mol = Chem.MolFromSmiles(charged_smiles)
            for atom, index in zip(charged_mol.GetAtoms(), indices_to_original_mol):
                CCD_mol.GetAtoms()[index].SetFormalCharge(atom.GetFormalCharge())
            ligand_mol = Chem.MolFromPDBFile(f"{self.data_dir}/ligand_{i}.pdb", removeHs=True)
            align = Chem.rdMolAlign.GetO3A(CCD_mol, ligand_mol)

            # editable_ligand_mol = Chem.EditableMol(ligand_mol)
            # for bond in ligand_mol.GetBonds():
            #     editable_ligand_mol.RemoveBond(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
            # for bond in CCD_mol.GetBonds():
            #     editable_ligand_mol.AddBond(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), bond.GetBondType())
            # ligand_mol = editable_ligand_mol.GetMol()
            # Chem.SanitizeMol(ligand_mol)
            # align = Chem.rdMolAlign.GetCrippenO3A(CCD_mol, ligand_mol)
            if len(align.Matches()) != len(ligand_mol.GetAtoms())



            for match in align.Matches():
                print(match)
            exit()


            for match in align.Matches():
                ideal_heteroresidue_atom, heteroresidue_atom = match
                print(match)

                list(res.get_atoms())[heteroresidue_atom-1].dimorphite_dl_charge = CCD_mol.GetAtoms()[ideal_heteroresidue_atom-1].GetFormalCharge()
            exit()

            for atom in res.get_atoms():
                print(atom.dimorphite_dl_charge)

            exit()


            #centrum = res.center_of_mass(geometric=True)




        exit()

        biotite_protein = biotite_mmCIF.get_structure(self.biotite_mmCIF_file,
                                                      model=1,
                                                      extra_fields=["charge"],
                                                      include_bonds=True)




        biotite_protein = biotite_protein[biotite_protein.element != "H"]
        biotite_protein.bonds = biotite_structure.connect_via_residue_names(biotite_protein)
        bond_array = biotite_protein.bonds.as_array()
        unknown_order_mask = bond_array[:, 2] == biotite_structure.BondType.ANY
        if unknown_order_mask.any():
            bond_array[unknown_order_mask, 2] = biotite_structure.BondType.SINGLE
            biotite_protein.bonds = biotite_protein.BondList(biotite_protein.array_length(), bond_array)
        biotite_protein_with_hydrogens, _ = hydride.add_hydrogen(biotite_protein, mask=biotite_protein.hetero)
        biotite_mmCIF_file_with_hydrogens = biotite_mmCIF.CIFFile()
        biotite_mmCIF.set_structure(biotite_mmCIF_file_with_hydrogens, biotite_protein_with_hydrogens)
        self.biotite_mmCIF_file.block["atom_site"] = biotite_mmCIF_file_with_hydrogens.block["atom_site"]


        self.biotite_mmCIF_file.write(f"{self.data_dir}/protonated_ligands.cif")

        # these three lines can be probably removed, after biotite 1.0.2 will be released
        mmcif_string = open(f"{self.data_dir}/protonated_ligands.cif").read()
        repaired_mmcif_string = mmcif_string.replace("\n# ", "\n# \n")
        open(f"{self.data_dir}/protonated_ligands.cif", "w").write(repaired_mmcif_string)
        exit()
        print("ok")


    def protonate_protein(self):
        print("Adding hydrogens to protein... ", end="")
        molecule = molit.Molecule(f"{self.data_dir}/protonated_ligands.cif")
        prepared_molecule = systemPrepare(molecule, pH=self.pH, ignore_ns_errors=True)
        prepared_molecule.write(f"{self.data_dir}/protonated_protein.cif", )
        print("ok")
        return prepared_molecule.charge
        exit()



if __name__ == "__main__":
    args = load_arguments()
    ChargesCalculator(args.mmCIF_file, args.data_dir, args.pH).calculate_charges()

# dotazy na chlapy
# stahovat vždy jedno pdb a nebo stáhnout celou PDB a ?
# jak cesty k pdb2pqr, hydride, xtb?
# výsledky potřebujeme někam uložit aby si to pak webovka mohla tahat
# uděláme testovací run? Třeba 1000 struktur?
# pořešit, zda to jde na vícekrát?




# možná nepůjde stáhnout všechno
# určitě log pro všechny rezidua
# nechat si vždycky verzi, se kteoru se pracovalo
