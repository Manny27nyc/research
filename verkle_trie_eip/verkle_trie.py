from bandersnatch import Point, Scalar
import hashlib
from random import randint, shuffle, choice
from poly_utils import PrimeField
from time import time
from ipa_utils import IPAUtils, hash
import sys

#
# Proof of concept implementation for verkle tries
#
# All polynomials in this implementation are represented in evaluation form, i.e. by their values
# on primefield.DOMAIN. 
#
# Ethereum-specific implementation according to this EIP draft:
# https://notes.ethereum.org/uwK4EJypSHWyEZvivcYyJA
#

# Bandersnatch curve modulus
MODULUS = 13108968793781547619861935127046491459309155893440570251786403306729687672801

# Verkle trie parameters
KEY_LENGTH = 256 # bits
WIDTH_BITS = 8
WIDTH = 2**WIDTH_BITS

primefield = PrimeField(MODULUS, WIDTH)

# Number of key-value pairs to insert
NUMBER_STEMS = 2**15
CHUNKS_PER_STEM = 10
# Needs to be less than WIDTH * NUMBER_STEMS
NUMBER_CHUNKS = CHUNKS_PER_STEM * NUMBER_STEMS

# Number of extra stems to add to tree
NUMBER_ADDED_STEMS = 512

# Number of chunks to add to existing stems
NUMBER_ADDED_CHUNKS = 512

# Number of actually existing key/values pair in proof
NUMBER_EXISTING_KEYS_PROOF = 5000

# Added stems and chunks randomly (most likely empty)
NUMBER_RANDOM_STEMS_PROOF = 500
NUMBER_RANDOM_CHUNKS_PROOF = 500

NUMBER_VALUES_CHANGED = 300

# Verkle trie constants
VERKLE_TRIE_NODE_TYPE_INNER = 0
VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE = 1


# Verkle proof wire constants
VERKLE_PROOF_COMMITMENT_TYPE_INNER = 0
VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION = 1
VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1 = 2
VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2 = 3

VERKLE_PROOF_EXTENSION_PRESENT_NOEXTENSION = 0
VERKLE_PROOF_EXTENSION_PRESENT_PRESENT = 1
VERKLE_PROOF_EXTENSION_PRESENT_OTHERSTEM = 2 # Used to indicate that there is an extension present, but for a different stem

def generate_basis(size):
    """
    Generates a basis for Pedersen commitments
    """
    # TODO: Currently random points that differ on every run.
    # Implement reproducable basis generation once hash_to_curve is provided
    BASIS_G = [Point(generator=False) for i in range(WIDTH)]
    BASIS_Q = Point(generator=False)
    return {"G": BASIS_G, "Q": BASIS_Q}


def get_stem(key):
    return key[:31]


def get_suffix(key):
    return key[31]


def commitment_to_field(commitment):
    return int.from_bytes(commitment.serialize(), "little") % MODULUS


# Node types: (Represented as python dicts)
#
# VERKLE_TRIE_NODE_TYPE_INNER:
#   0-255 refs to child node
#   "commitment": commitment
#   "commitment_field": commitment % MODULUS
#
# VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE:
#   0-255 values (as bytes)
#   "stem": stem (31 bytes)
#   "C1": C1
#   "C1_field": C1 % MODULUS
#   "C2": C2
#   "C2_field": C2 % MODULUS
#   "commitment": commitment
#   "commitment_field": commitment % MODULUS


def update_verkle_tree_nocommitmentupdate(root_node, key, value):
    """
    Insert node without updating commitments (useful for building a full trie and adding commitments at the end)
    """

    current_node = root_node
    stem = get_stem(key)
    suffix = get_suffix(key)
    index = None
    depth = 0

    while current_node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:
        previous_node = current_node
        previous_index = index
        index = stem[depth]
        depth += 1
        if index in current_node:
            current_node = current_node[index]
        else:
            current_node[index] = {"node_type": VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE, "stem": stem, suffix: value}
            return

    if current_node["stem"] == stem:
        current_node[suffix] = value
    else:
        old_suffix_tree = current_node
        old_stem = old_suffix_tree["stem"]

        new_inner_node = {"node_type": VERKLE_TRIE_NODE_TYPE_INNER}
        previous_node[index] = new_inner_node
        previous_node = new_inner_node
        
        while old_stem[depth] == stem[depth]:
            index = stem[depth]
            new_inner_node = {"node_type": VERKLE_TRIE_NODE_TYPE_INNER}
            previous_node[index] = new_inner_node
            previous_node = new_inner_node
            depth += 1

        new_inner_node[stem[depth]] = {"node_type": VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE, "stem": stem, suffix: value}
        new_inner_node[old_stem[depth]] = old_suffix_tree


def update_verkle_tree(root_node, key, value):
    """
    Update or insert node and update all commitments
    """
    current_node = root_node
    index = None
    path = []
    stem = get_stem(key)
    suffix = get_suffix(key)

    while True:
        index = stem[len(path)]
        path.append((index, current_node))
        if index in current_node:
            if current_node[index]["node_type"] == VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE:
                old_node = current_node[index]
                if current_node[index]["stem"] == stem:
                    current_node = current_node[index]
                    old_value_lower = (int.from_bytes(current_node[suffix][:16], "little") + 2**128) if suffix in current_node else 0
                    old_value_upper = (int.from_bytes(current_node[suffix][16:], "little")) if suffix in current_node else 0
                    current_node[suffix] = value
                    new_value_lower = int.from_bytes(value[:16], "little") + 2**128
                    new_value_upper = int.from_bytes(value[16:], "little")
                    commitment_change = BASIS["G"][2 * suffix % 256].dup().mul((MODULUS + new_value_lower - old_value_lower) % MODULUS) \
                                        .add(BASIS["G"][(2 * suffix + 1) % 256].dup().mul((MODULUS + new_value_upper - old_value_upper) % MODULUS))
                    
                    if suffix < 128:
                        current_node["C1"].add(commitment_change)
                        new_field = commitment_to_field(current_node["C1"])
                        current_node["commitment"].add(BASIS["G"][2].dup().mul((MODULUS + new_field - current_node["C1_field"]) % MODULUS))
                        current_node["C1_field"] = new_field
                    else:
                        current_node["C2"].add(commitment_change)
                        new_field = commitment_to_field(current_node["C2"])
                        current_node["commitment"].add(BASIS["G"][3].dup().mul((MODULUS + new_field - current_node["C2_field"]) % MODULUS))
                        current_node["C2_field"] = new_field
                    new_field = commitment_to_field(current_node["commitment"])
                    value_change = (MODULUS + new_field - current_node["commitment_field"]) % MODULUS
                    current_node["commitment_field"] = new_field
                    break
                else:
                    new_inner_node = {"node_type": VERKLE_TRIE_NODE_TYPE_INNER}
                    new_index = stem[len(path)]
                    old_index = old_node["stem"][len(path)]
                    current_node[index] = new_inner_node

                    inserted_path = []
                    current_node = new_inner_node
                    while old_index == new_index:
                        index = new_index
                        next_inner_node = {"node_type": VERKLE_TRIE_NODE_TYPE_INNER}
                        current_node[index] = next_inner_node
                        inserted_path.append((index, current_node))
                        new_index = stem[len(path) + len(inserted_path)]
                        old_index = old_node["stem"][len(path) + len(inserted_path)]
                        current_node = next_inner_node

                    current_node[new_index] = {"node_type": VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE, "stem": stem, suffix: value}
                    current_node[old_index] = old_node

                    verkle_add_missing_commitments(current_node)
                    for index, node in reversed(inserted_path):
                        verkle_add_missing_commitments(node)

                    value_change = (MODULUS + new_inner_node["commitment_field"] - old_node["commitment_field"]) % MODULUS
                    break

            current_node = current_node[index]
        else:
            current_node[index] = {"node_type": VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE, "stem": stem, suffix: value}
            verkle_add_missing_commitments(current_node[index])
            value_change = current_node[index]["commitment_field"]
            break
    
    # Update all the parent commitments along 'path'
    for index, node in reversed(path):
        node["commitment"].add(BASIS["G"][index].dup().mul(value_change))
        old_field = node["commitment_field"]
        new_field = commitment_to_field(node["commitment"])
        node["commitment_field"] = new_field
        value_change = (MODULUS + new_field - old_field) % MODULUS


def verkle_add_missing_commitments(node):
    """
    Recursively adds all missing commitments and hashes to a verkle trie structure.
    """
    if node["node_type"] == VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE:

        C1 = ipa_utils.pedersen_commit_sparse({2 * i + j: int.from_bytes(node[i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128
                                                for i in range(128)
                                                for j in range(2)
                                                if i in node})

        C2 = ipa_utils.pedersen_commit_sparse({2 * i + j: int.from_bytes(node[128 + i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128
                                                for i in range(128)
                                                for j in range(2)
                                                if 128 + i in node})

        C1_field = commitment_to_field(C1)
        C2_field = commitment_to_field(C2)

        node["C1"] = C1
        node["C1_field"] = C1_field
        node["C2"] = C2
        node["C2_field"] = C2_field

        commitment = ipa_utils.pedersen_commit_sparse({0: 1, 
                                                       1: int.from_bytes(node["stem"], "little"),
                                                       2: C1_field, 
                                                       3: C2_field})

        node["commitment"] = commitment
        node["commitment_field"] = commitment_to_field(commitment)

    elif node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:
        child = {}
        for i in range(WIDTH):
            if i in node:
                if "commitment_field" not in node[i]:
                    verkle_add_missing_commitments(node[i])
                child[i] = node[i]["commitment_field"]
        commitment = ipa_utils.pedersen_commit_sparse(child)
        node["commitment"] = commitment
        node["commitment_field"] = int.from_bytes(commitment.serialize(), "little") % MODULUS


def check_valid_tree(node, prefix=b""):
    """
    Checks that the subtree starting at `node` with prefix `prefix` is valid.
    Returns all the values found in the tree as a dict.
    """
    values = {}
    if node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:

        child = {}
        for i in range(WIDTH):
            if i in node:
                child[i] = node[i]["commitment_field"]
        commitment = ipa_utils.pedersen_commit_sparse(child)
        assert node["commitment"] == commitment
        assert node["commitment_field"] == int.from_bytes(commitment.serialize(), "little") % MODULUS

        for i in range(WIDTH):
            if i in node:
                values.update(check_valid_tree(node[i], prefix + bytes([i])))
    else:
        assert node["node_type"] == VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE
        assert node["stem"][:len(prefix)] == prefix
        C1 = ipa_utils.pedersen_commit_sparse({2 * i + j: int.from_bytes(node[i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128
                                                for i in range(128)
                                                for j in range(2)
                                                if i in node})

        C2 = ipa_utils.pedersen_commit_sparse({2 * i + j: int.from_bytes(node[128 + i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128
                                                for i in range(128)
                                                for j in range(2)
                                                if 128 + i in node})

        C1_field = commitment_to_field(C1)
        C2_field = commitment_to_field(C2)

        assert node["C1"] == C1
        assert node["C1_field"] == C1_field
        assert node["C2"] == C2
        assert node["C2_field"] == C2_field

        commitment = ipa_utils.pedersen_commit_sparse({0: 1, 
                                                       1: int.from_bytes(node["stem"], "little"),
                                                       2: C1_field, 
                                                       3: C2_field})

        assert node["commitment"] == commitment
        assert node["commitment_field"] == commitment_to_field(commitment)

        for i in range(WIDTH):
            if i in node:
                values[node["stem"] + bytes([i])] = node[i]
    
    return values


def get_total_depth(node):
    """
    Computes the total depth (sum of the depth of all nodes) of a verkle trie as well as the total number of nonzero values.
    Depth refers to the total number of commitments on the path to revealing the values, including the root and the
    extension and suffix tree node. (So a single value in an otherwise empty verkle tree has depth 3:
    root + extension + suffix_tree)
    """
    if node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:
        total_depth = 0
        num_nodes = 0
        for i in range(WIDTH):
            if i in node:
                depth, nodes = get_total_depth(node[i])
                num_nodes += nodes
                total_depth += nodes + depth
        return total_depth, num_nodes
    else:
        num_chunks = len([i for i  in range(256) if i in node])
        return num_chunks * 2, num_chunks


def get_average_depth(root_node):
    """
    Get the average depth of nodes in a verkle trie
    """
    depth, nodes = get_total_depth(root_node)
    return depth / nodes


def find_node_with_path(root_node, key):
    """
    Returns the path of all nodes on the way to 'key' as well as their index
    """
    current_node = root_node
    path = []
    stem = get_stem(key)
    while current_node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:
        index = key[len(path)]
        path.append((stem[:len(path)], index, current_node))
        if index in current_node:
            current_node = current_node[index]
        else:
            return path, None

    if current_node["stem"] == stem:
        suffix = get_suffix(key)
        path.append((stem, suffix, current_node))
        if suffix in current_node:
            return path, current_node[suffix]

    return path, None
    

def get_proof_size(proof):
    depths, extension_present, commitments_sorted_by_index_serialized, other_stems, D_serialized, ipa_proof = proof
    size = len(depths) # assume 8 bit integer to represent the depth (5 bit) and extension_present(2 bit)
    size += 32 * len(commitments_sorted_by_index_serialized)
    size += 32 * len(other_stems)
    size += 32 + (len(ipa_proof) - 1) * 2 * 32 + 32
    return size


lasttime = [0]


def start_logging_time_if_eligible(string, eligible):
    if eligible:
        print(string, file=sys.stderr)
        lasttime[0] = time()


def log_time_if_eligible(string, width, eligible):
    if eligible:
        print(string + ' ' * max(1, width - len(string)) + "{0:7.3f} s".format(time() - lasttime[0]), file=sys.stderr)
        lasttime[0] = time()


def make_ipa_multiproof(Cs, fs, zs, ys, display_times=True):
    """
    Computes an IPA multiproof according to the schema described here:
    https://dankradfeist.de/ethereum/2021/06/18/pcs-multiproofs.html

    This proof makes the assumption that the domain is the integers 0, 1, 2, ... WIDTH - 1
    """

    # Step 1: Construct g(X) polynomial in evaluation form
    r = ipa_utils.hash_to_field(Cs + zs + ys) % MODULUS

    log_time_if_eligible("   Hashed to r", 30, display_times)

    g = [0 for i in range(WIDTH)]
    power_of_r = 1
    for f, index in zip(fs, zs):
        quotient = primefield.compute_inner_quotient_in_evaluation_form(f, index)
        for i in range(WIDTH):
            g[i] += power_of_r * quotient[i]

        power_of_r = power_of_r * r % MODULUS
    
    for i in range(len(g)):
        g[i] %= MODULUS

    log_time_if_eligible("   Computed g polynomial", 30, display_times)

    D = ipa_utils.pedersen_commit(g)

    log_time_if_eligible("   Computed commitment D", 30, display_times)

    # Step 2: Compute h in evaluation form
    
    t = ipa_utils.hash_to_field([r, D]) % MODULUS
    
    h = [0 for i in range(WIDTH)]
    power_of_r = 1
    
    for f, index in zip(fs, zs):
        denominator_inv = primefield.inv(t - primefield.DOMAIN[index])
        for i in range(WIDTH):
            h[i] += power_of_r * f[i] * denominator_inv % MODULUS
            
        power_of_r = power_of_r * r % MODULUS
   
    for i in range(len(h)):
        h[i] %= MODULUS

    log_time_if_eligible("   Computed h polynomial", 30, display_times)

    h_minus_g = [(h[i] - g[i]) % primefield.MODULUS for i in range(WIDTH)]

    # Step 3: Evaluate and compute IPA proofs

    E = ipa_utils.pedersen_commit(h)

    y, ipa_proof = ipa_utils.evaluate_and_compute_ipa_proof(E.dup().add(D.dup().mul(MODULUS-1)), h_minus_g, t)

    log_time_if_eligible("   Computed IPA proof", 30, display_times)

    return D.serialize(), ipa_proof


def check_ipa_multiproof(Cs, zs, ys, proof, display_times=True):
    """
    Verifies an IPA multiproof according to the schema described here:
    https://dankradfeist.de/ethereum/2021/06/18/pcs-multiproofs.html
    """

    D_serialized, ipa_proof = proof

    D = Point().deserialize(D_serialized)

    # Step 1
    r = ipa_utils.hash_to_field(Cs + zs + ys)

    log_time_if_eligible("   Computed r hash", 30, display_times)
    
    # Step 2
    t = ipa_utils.hash_to_field([r, D])
    E_coefficients = []
    g_2_of_t = 0
    power_of_r = 1

    for index, y in zip(zs, ys):
        E_coefficient = primefield.div(power_of_r, t - primefield.DOMAIN[index])
        E_coefficients.append(E_coefficient)
        g_2_of_t += E_coefficient * y % MODULUS
            
        power_of_r = power_of_r * r % MODULUS

    log_time_if_eligible("   Computed g2 and e coeffs", 30, display_times)
    
    # TODO: Deduplicate Cs in order to make this MSM faster
    E = Point().msm(Cs, E_coefficients)

    log_time_if_eligible("   Computed E commitment", 30, display_times)

    # Step 3 (Check IPA proofs)
    y = g_2_of_t % primefield.MODULUS

    if not ipa_utils.check_ipa_proof(E.dup().add(D.dup().mul(MODULUS - 1)), t, y, ipa_proof):
        return False

    log_time_if_eligible("   Checked IPA proof", 30, display_times)

    return True


def make_verkle_proof(trie, keys, display_times=True):
    """
    Creates a proof for the `keys` in the verkle trie given by `trie`

    This includes proving that a value is not in the verkle trie
    """

    start_logging_time_if_eligible("   Starting proof computation", display_times)

    #
    # Revealing a full verkle proof requires the following proofs for each `key`
    #
    # - all nodes on the path to the extension node, subindex determined by stem           [VERKLE_PROOF_COMMITMENT_TYPE_INNER]
    # 
    # If there is an extension at the last place, then further it requires an extension proof:
    #
    # - the extension node, subindex 0 (1)                                                 [VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION]
    # - the extension node, subindex 1 (stem)                                              [VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION]
    #
    # The stem of the extension can be a different stem, which has to be provided (either it is already in another key or it has to be added
    # to the proof)
    # If the stem is the stem of `key`, then the suffix tree for the suffix has to be revealed:
    #
    # - the extension node, subindex 2/3 for C1/C2 (if index <128/>=128)                   [VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION]
    # - the suffix tree node C1/C2, subindex 2 * suffix % 128     (value_lower + 2**128)   [VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C{1/2}]
    # - the suffix tree node C1/C2, subindex 2 * suffix + 1 % 128 (value_upper)            [VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C{1_2}]
    #

    # Step 0: Find all keys in the trie

    # Nodes by index -- index refers to the combination of commitment_type (VERKLE_PROOF_COMMITMENT_TYPE_INNER,
    # VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C{1/2}) and the prefix.
    nodes_by_index = {}

    # Nodes by index and z -- z is the position of the node that is opened
    nodes_by_index_and_subindex = {}

    # All values in order of keys. "None" is used for never written values and b"\0" * 32 for zero/deleted values
    values = []

    # Depth at which the extension node for the stem was found
    depths_by_stem = {}

    # Whether or not a given stem had an extension node or not
    extension_present_by_stem = {}

    key_stems = set(get_stem(key) for key in keys)

    # Where a different stem was encountered, we need to record its value for the proof
    other_stems = set()

    for key in keys:
        path, value = find_node_with_path(trie, key)
        values.append(value)
        for prefix, subindex, node in path:
            if node["node_type"] == VERKLE_TRIE_NODE_TYPE_INNER:
                nodes_by_index[(VERKLE_PROOF_COMMITMENT_TYPE_INNER, prefix)] = node
                nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_INNER, prefix, subindex)] = node
        
        if path[-1][2]["node_type"] == VERKLE_TRIE_NODE_TYPE_SUFFIX_TREE:
            stem = path[-1][0]
            suffix = path[-1][1]
            node = path[-1][2]
            extension_present_by_stem[stem] = VERKLE_PROOF_EXTENSION_PRESENT_PRESENT
            depths_by_stem[stem] = len(path) - 1

            nodes_by_index[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem)] = node
            nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 0)] = node # 1
            nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 1)] = node # stem
            nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 2 + suffix // 128)] = node # C1/C2

            suffix_tree_commitment = VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1 if suffix < 128 else VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2
            nodes_by_index[(suffix_tree_commitment, stem)] = node
            nodes_by_index_and_subindex[(suffix_tree_commitment, stem, suffix * 2 % 256)] = node # value_lower
            nodes_by_index_and_subindex[(suffix_tree_commitment, stem, (suffix * 2 + 1) % 256)] = node # value_upper
        else:
            stem = get_stem(key)
            depths_by_stem[stem] = len(path)
            node = path[-1][2]

            if path[-1][1] in node:
                node = node[path[-1][1]]
                extension_present_by_stem[stem] = VERKLE_PROOF_EXTENSION_PRESENT_OTHERSTEM
                other_stem = node["stem"]
                nodes_by_index[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem)] = node
                nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 0)] = node # 1
                nodes_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 1)] = node # stem
                other_stems.add(other_stem)
            else:
                extension_present_by_stem[stem] = VERKLE_PROOF_EXTENSION_PRESENT_NOEXTENSION

    depths = list(map(lambda x: x[1], sorted(depths_by_stem.items())))
    extension_present = list(map(lambda x: x[1], sorted(extension_present_by_stem.items())))
        
    log_time_if_eligible("   Computed key paths", 30, display_times)
    
    # Nodes sorted 
    nodes_sorted_by_index_and_subindex = sorted(nodes_by_index_and_subindex.items())
    
    log_time_if_eligible("   Sorted all commitments", 30, display_times)
    
    indices = []
    ys = []
    fs = []
    Cs = []

    for index_and_subindex, node in nodes_sorted_by_index_and_subindex:
        node_type, index, subindex = index_and_subindex
        indices.append(subindex)
        if node_type == VERKLE_PROOF_COMMITMENT_TYPE_INNER:
            Cs.append(node["commitment"])
            ys.append(node[subindex]["commitment_field"] if subindex in node else 0)
            fs.append([node[i]["commitment_field"] if i in node else 0 for i in range(WIDTH)])
        elif node_type == VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION:
            Cs.append(node["commitment"])
            if subindex == 0:
                ys.append(1)
            elif subindex == 1:
                ys.append(int.from_bytes(node["stem"], "little"))
            elif subindex == 2:
                ys.append(node["C1_field"])
            elif subindex == 3:
                ys.append(node["C2_field"])
            fs.append([1, 
                       int.from_bytes(node["stem"], "little"),
                       node["C1_field"],
                       node["C2_field"]]
                        + [0] * 252)
        elif node_type == VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1:
            Cs.append(node["C1"])
            suffix = subindex // 2
            if suffix not in node:
                ys.append(0)
            else:
                if subindex % 2 == 0:
                    ys.append(int.from_bytes(node[suffix][:16], "little") + 2**128)
                else:
                    ys.append(int.from_bytes(node[suffix][16:], "little"))
            fs.append([(int.from_bytes(node[i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128) if i in node else 0
                                                for i in range(128)
                                                for j in range(2)])
        elif node_type == VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2:
            Cs.append(node["C2"])
            suffix = 128 + subindex // 2
            if suffix not in node:
                ys.append(0)
            else:
                if subindex % 2 == 0:
                    ys.append(int.from_bytes(node[suffix][:16], "little") + 2**128)
                else:
                    ys.append(int.from_bytes(node[suffix][16:], "little"))
            fs.append([(int.from_bytes(node[128 + i][16 * j:16 * (j + 1)], "little") + (1 - j) * 2**128) if 128 + i in node else 0
                                                for i in range(128)
                                                for j in range(2)])


    D_serialized, ipa_proof = make_ipa_multiproof(Cs, fs, indices, ys, display_times)

    # All commitments, but without any duplications. These are for sending over the wire as part of the proof
    nodes_sorted_by_index = sorted(nodes_by_index.items())
    commitments_sorted_by_index_serialized = []
    for index, node in nodes_sorted_by_index[1:]:
        if index[0] in [VERKLE_PROOF_COMMITMENT_TYPE_INNER, VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION]:
            commitments_sorted_by_index_serialized.append(node["commitment"].serialize())
        elif index[0] == VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1:
            commitments_sorted_by_index_serialized.append(node["C1"].serialize())
        elif index[0] == VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2:
            commitments_sorted_by_index_serialized.append(node["C2"].serialize())
    
    stems_with_extension = set(stem for stem in extension_present_by_stem if extension_present_by_stem[stem])
    other_stems = sorted(list(stem for stem in other_stems if stem not in stems_with_extension))
    
    log_time_if_eligible("   Serialized commitments", 30, display_times)

    global YS_TO_CHECK
    YS_TO_CHECK = ys

    return depths, extension_present, commitments_sorted_by_index_serialized, other_stems, D_serialized, ipa_proof


def check_verkle_proof(trie, keys, values, updated_values, new_verkle_root, proof, display_times=True):
    """
    Checks Verkle tree proof according to
    https://dankradfeist.de/ethereum/2021/06/18/pcs-multiproofs.html

    Updated_values contains new updated values. Can be "None" for any value that does not need updating.
    Checks that the resulting root is "new_verkle_root"
    """

    start_logging_time_if_eligible("   Starting proof check", display_times)

    # Unpack the proof
    depths, extension_present, commitments_sorted_by_index_serialized, other_stems, D_serialized, ipa_proof = proof
    commitments_sorted_by_index = [Point().deserialize(trie)] + [Point().deserialize(x) for x in commitments_sorted_by_index_serialized]

    all_indices = set()
    all_indices_and_subindices = set()
    
    leaf_values_by_index_and_subindex = {}

    # Find all stems
    stems = sorted(list(set([get_stem(key) for key in keys])))
    depths_by_stem = {}
    extension_present_by_stem = {}
    stem_by_unique_prefix = {}
    stems_with_extension = set()

    for stem, depth, extpres in zip(stems, depths, extension_present):
        depths_by_stem[stem] = depth
        
        extension_present_by_stem[stem] = extpres
        if extpres == VERKLE_PROOF_EXTENSION_PRESENT_PRESENT:
            stems_with_extension.add(stem)
            stem_by_unique_prefix[stem[:depth]] = stem
        elif extpres == VERKLE_PROOF_EXTENSION_PRESENT_NOEXTENSION:
            stem_by_unique_prefix[stem[:depth]] = stem
        elif extpres == VERKLE_PROOF_EXTENSION_PRESENT_OTHERSTEM:
            other_stem = None
            for o in other_stems:
                if o[:depth] == stem[:depth]:
                    assert other_stem is None
                    other_stem = o
            if other_stem is not None:
                stem_by_unique_prefix[stem[:depth]] = other_stem
    
    # Find all required indices
    for key, value in zip(keys, values):
        stem = get_stem(key)
        depth = depths_by_stem[stem]
        extpres = extension_present_by_stem[stem]
        for i in range(depth):
            all_indices.add((VERKLE_PROOF_COMMITMENT_TYPE_INNER, stem[:i]))
            all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_INNER, stem[:i], stem[i]))

        if extpres == VERKLE_PROOF_EXTENSION_PRESENT_PRESENT:
            suffix = get_suffix(key)

            all_indices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem))
            all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 0))
            all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 1))
            all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 2 + (suffix // 128)))

            leaf_values_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 0)] = 1
            leaf_values_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem, 1)] = int.from_bytes(stem, "little")

            suffix_tree_commitment = VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1 if suffix < 128 else VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2
            all_indices.add((suffix_tree_commitment, stem))
            all_indices_and_subindices.add((suffix_tree_commitment, stem, 2 * suffix % 256))
            all_indices_and_subindices.add((suffix_tree_commitment, stem, (2 * suffix + 1) % 256))

            value_lower = int.from_bytes(value[:16], "little") + 2**128 if value != None else 0
            value_upper = int.from_bytes(value[16:], "little") if value != None else 0

            leaf_values_by_index_and_subindex[(suffix_tree_commitment, stem, 2 * suffix % 256)] = value_lower
            leaf_values_by_index_and_subindex[(suffix_tree_commitment, stem, (2 * suffix + 1) % 256)] = value_upper
        elif extpres == VERKLE_PROOF_EXTENSION_PRESENT_OTHERSTEM:
            # The proof indicates that an extension node for a different stem was found in the tree
            # We need to verify this is the case by looking up the other stem in "other_stems"
            # and verifying that the extension node at the site is indeed for the other stem

            # First check if the extension node is already included in the proof because the other stem
            # is already part of this proof. In this special case, we don't have to do anything because the 
            # extension proof for the other stem will already do all the work.
            other_stem = None

            # The stem not having an extension node means that the value has never been set:
            if value is not None:
                return False

            # TODO: Convert this to binary search to prevent DOS vectors
            for o in stems_with_extension:
                if o[:depth] == stem[:depth]:
                    assert other_stem is None
                    other_stem = o

            if other_stem is None:
                # TODO: other_stem search is linear search which should work perfectly in average case but
                # is a DOS vector. Need to employ binary search
                for o in other_stems:
                    if o[:depth] == stem[:depth]:
                        assert other_stem is None
                        other_stem = o

                # Now we need to add this extension node to the proof to show that our original stem wasn't
                # present
                all_indices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem))
                all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 0))
                all_indices_and_subindices.add((VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 1))

                leaf_values_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 0)] = 1
                leaf_values_by_index_and_subindex[(VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, other_stem, 1)] = int.from_bytes(other_stem, "little")
        elif extpres == VERKLE_PROOF_EXTENSION_PRESENT_NOEXTENSION:
            # Prover can only claim extension is not present if value was never written
            if value is not None:
                return False
        else:
            # Invalid value for extpres
            return False
    
    all_indices = sorted(all_indices)
    assert len(all_indices) == len(commitments_sorted_by_index)
    all_indices_and_subindices = sorted(all_indices_and_subindices)

    log_time_if_eligible("   Computed indices", 30, display_times)

    # Step 0: recreate the commitment list sorted by indices
    commitments_by_index = {index: commitment for index, commitment in zip(all_indices, commitments_sorted_by_index)}
    commitments_by_index_and_subindex = {index_and_subindex: commitments_by_index[index_and_subindex[:2]]
                                            for index_and_subindex in all_indices_and_subindices}
    
    ys_by_index_and_subindex = {}
    for index_and_subindex in all_indices_and_subindices:
        if index_and_subindex[0] == VERKLE_PROOF_COMMITMENT_TYPE_INNER:
            
            child_index = (VERKLE_PROOF_COMMITMENT_TYPE_INNER, index_and_subindex[1] + bytes([index_and_subindex[2]]))
            if child_index in commitments_by_index:
                ys_by_index_and_subindex[index_and_subindex] = int.from_bytes(commitments_by_index[child_index].serialize(), "little") % MODULUS
            else:
                stem = stem_by_unique_prefix[index_and_subindex[1] + bytes([index_and_subindex[2]])]
                if stem not in extension_present_by_stem or extension_present_by_stem[stem] == VERKLE_PROOF_EXTENSION_PRESENT_PRESENT:
                    child_index = (VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION, stem)
                    ys_by_index_and_subindex[index_and_subindex] = int.from_bytes(commitments_by_index[child_index].serialize(), "little") % MODULUS
                else:
                    ys_by_index_and_subindex[index_and_subindex] = 0
        elif index_and_subindex[0] == VERKLE_PROOF_COMMITMENT_TYPE_EXTENSION:
            if index_and_subindex[2] < 2:
                ys_by_index_and_subindex[index_and_subindex] = leaf_values_by_index_and_subindex[index_and_subindex]
            else:
                suffix_tree_commitment = VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1 if index_and_subindex[2] == 2 else VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2
                child_index = (suffix_tree_commitment, index_and_subindex[1])
                ys_by_index_and_subindex[index_and_subindex] = int.from_bytes(commitments_by_index[child_index].serialize(), "little") % MODULUS
        elif index_and_subindex[0] in [VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C1, VERKLE_PROOF_COMMITMENT_TYPE_SUFFIX_TREE_C2]:
            ys_by_index_and_subindex[index_and_subindex] = leaf_values_by_index_and_subindex[index_and_subindex]
    
    Cs = list(map(lambda x: x[1], sorted(commitments_by_index_and_subindex.items())))
    
    indices = list(map(lambda x: x[2], sorted(all_indices_and_subindices)))
    
    ys = list(map(lambda x: x[1], sorted(ys_by_index_and_subindex.items())))

    log_time_if_eligible("   Recreated commitment lists", 30, display_times)

    global YS_TO_CHECK

    for i, pair in enumerate(zip(YS_TO_CHECK, ys)):
        if pair[0] != pair[1]:
            print(i, pair)
            print(all_indices_and_subindices[i])
            stem = all_indices_and_subindices[i][1]
            suffix = all_indices_and_subindices[i][2] // 2
            key = stem + bytes([suffix])
            print(key, suffix, values[keys.index(key)])
            path, value = find_node_with_path(root, key)
            print(value)

    assert check_ipa_multiproof(Cs, indices, ys, [D_serialized, ipa_proof], display_times)
    
    # TODO: Process updates

    return True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        WIDTH_BITS = int(sys.argv[1])
        WIDTH = 2 ** WIDTH_BITS
        ROOT_OF_UNITY = pow(PRIMITIVE_ROOT, (MODULUS - 1) // WIDTH, MODULUS)
        primefield.DOMAIN = [pow(ROOT_OF_UNITY, i, MODULUS) for i in range(WIDTH)]

        NUMBER_INITIAL_KEYS = int(sys.argv[2])

        NUMBER_KEYS_PROOF = int(sys.argv[3])

        NUMBER_DELETED_KEYS = 0
        NUMBER_ADDED_KEYS = 0
    
    BASIS = generate_basis(WIDTH)
    ipa_utils = IPAUtils(BASIS["G"], BASIS["Q"], primefield)

    # Build a random verkle trie
    root = {"node_type": VERKLE_TRIE_NODE_TYPE_INNER}

    values = {}

    for i in range(NUMBER_STEMS):
        stem = randint(0, 2**248-1).to_bytes(31, "little")
        for i in range(CHUNKS_PER_STEM):
            key = stem + bytes([randint(0, 2**8-1)])
            value = randint(0, 2**256-1).to_bytes(32, "little")
            update_verkle_tree_nocommitmentupdate(root, key, value)
            values[key] = value
    
    average_depth = get_average_depth(root)
        
    print("Inserted {0} elements for an average depth of {1:.3f}".format(NUMBER_CHUNKS, average_depth), file=sys.stderr)
    print("Average depth = {0:.3f} without counting suffix trees (stem tree only)".format(average_depth - 2), file=sys.stderr)

    time_a = time()
    verkle_add_missing_commitments(root)
    time_b = time()

    print("Computed verkle root in {0:.3f} s".format(time_b - time_a), file=sys.stderr)

    time_a = time()
    assert values == check_valid_tree(root)
    time_b = time()
    
    print("[Checked tree valid: {0:.3f} s]".format(time_b - time_a), file=sys.stderr)

    if NUMBER_ADDED_STEMS > 0:
        time_x = time()
        for i in range(NUMBER_ADDED_STEMS):
            key = randint(0, 2**256-1).to_bytes(32, "little")
            value = randint(0, 2**256-1).to_bytes(32, "little")
            update_verkle_tree(root, key, value)
            values[key] = value
        time_y = time()
            
        print("Additionally inserted {0} stems in {1:.3f} s".format(NUMBER_ADDED_STEMS, time_y - time_x), file=sys.stderr)
        print("Keys in tree now: {0}, average depth: {1:.3f}".format(get_total_depth(root)[1], get_average_depth(root)), file=sys.stderr)

        time_a = time()
        assert values == check_valid_tree(root)
        time_b = time()
        
        print("[Checked tree valid: {0:.3f} s]".format(time_b - time_a), file=sys.stderr)

    if NUMBER_ADDED_CHUNKS > 0:

        time_x = time()
        existing_keys = list(values.keys())
        for i in range(NUMBER_ADDED_CHUNKS):
            stem = get_stem(choice(existing_keys))
            suffix = randint(0, 255)
            key = stem + bytes([suffix])
            value = randint(0, 2**256-1).to_bytes(32, "little")
            update_verkle_tree(root, key, value)
            values[key] = value
        time_y = time()
            
        print("Additionally inserted {0} chunks in {1:.3f} s".format(NUMBER_ADDED_CHUNKS, time_y - time_x), file=sys.stderr)
        print("Keys in tree now: {0}, average depth: {1:.3f}".format(get_total_depth(root)[1], get_average_depth(root)), file=sys.stderr)

        time_a = time()
        assert values == check_valid_tree(root)
        time_b = time()
        
        print("[Checked tree valid: {0:.3f} s]".format(time_b - time_a), file=sys.stderr)

    for key, value in values.items():
        path, v2 = find_node_with_path(root, key)
        if value != v2:
            print(key, value, v2)
            print(path[-1])
    

    all_keys = list(values.keys())
    shuffle(all_keys)

    keys_in_proof = all_keys[:NUMBER_EXISTING_KEYS_PROOF]
    values_in_proof = [values[key] for key in keys_in_proof]

    for i in range(NUMBER_RANDOM_STEMS_PROOF):
        key = randint(0, 2**256-1).to_bytes(32, "little")
        keys_in_proof.append(key)
        values_in_proof.append(values[key] if key in values else None)

    for i in range(NUMBER_RANDOM_CHUNKS_PROOF):
        stem = get_stem(choice(existing_keys))
        suffix = randint(0, 255)
        key = stem + bytes([suffix])        
        keys_in_proof.append(key)
        values_in_proof.append(values[key] if key in values else None)

    time_a = time()
    proof = make_verkle_proof(root, keys_in_proof)
    time_b = time()
    
    proof_size = get_proof_size(proof)
    proof_time = time_b - time_a
    
    print("Computed proof for {0} keys (size = {1} bytes) in {2:.3f} s".format(len(keys_in_proof), proof_size, time_b - time_a), file=sys.stderr)

    time_a = time()
    assert check_verkle_proof(root["commitment"].serialize(), keys_in_proof, values_in_proof, [], 0, proof)
    time_b = time()
    check_time = time_b - time_a

    print("Checked proof in {0:.3f} s".format(time_b - time_a), file=sys.stderr)