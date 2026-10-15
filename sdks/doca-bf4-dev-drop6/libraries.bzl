# Static archive paths verified against pinned BF4 dev-drop6 RPM payloads.
# These imports do not resolve the complete static link closure.
LIBRARIES = {
    "argp": {
        "archive": "opt/mellanox/doca/lib64/libdoca_argp.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "common": {
        "archive": "opt/mellanox/doca/lib64/libdoca_common.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "dpdk-bridge": {
        "archive": "opt/mellanox/doca/lib64/libdoca_dpdk_bridge.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "flow": {
        "archive": "opt/mellanox/doca/lib64/libdoca_flow.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "flow-definitions": {
        "archive": "opt/mellanox/doca/lib64/libdoca_flow_definitions.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "flow-info-comp": {
        "archive": "opt/mellanox/doca/lib64/libdoca_flow_info_comp.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "flow-pipeline-loader": {
        "archive": "opt/mellanox/doca/lib64/libdoca_flow_pipeline_loader.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "flow-tune-server": {
        "archive": "opt/mellanox/doca/lib64/libdoca_flow_tune_server.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "mgmt": {
        "archive": "opt/mellanox/doca/lib64/libdoca_mgmt.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    },
    "nvhws": {
        "archive": "usr/lib64/libnvhws.a",
        "headers": [
            "usr/include"
        ]
    },
    "rte-acl": {
        "archive": "opt/mellanox/dpdk/lib64/librte_acl.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-argparse": {
        "archive": "opt/mellanox/dpdk/lib64/librte_argparse.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bbdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bbdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bitratestats": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bitratestats.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bpf": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bpf.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bus-auxiliary": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bus_auxiliary.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bus-pci": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bus_pci.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-bus-vdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_bus_vdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-cfgfile": {
        "archive": "opt/mellanox/dpdk/lib64/librte_cfgfile.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-cmdline": {
        "archive": "opt/mellanox/dpdk/lib64/librte_cmdline.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-common-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_common_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-compress-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_compress_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-compressdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_compressdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-crypto-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_crypto_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-cryptodev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_cryptodev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-dispatcher": {
        "archive": "opt/mellanox/dpdk/lib64/librte_dispatcher.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-distributor": {
        "archive": "opt/mellanox/dpdk/lib64/librte_distributor.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-dmadev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_dmadev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-eal": {
        "archive": "opt/mellanox/dpdk/lib64/librte_eal.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-efd": {
        "archive": "opt/mellanox/dpdk/lib64/librte_efd.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-ethdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_ethdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-eventdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_eventdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-fib": {
        "archive": "opt/mellanox/dpdk/lib64/librte_fib.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-gpudev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_gpudev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-graph": {
        "archive": "opt/mellanox/dpdk/lib64/librte_graph.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-gro": {
        "archive": "opt/mellanox/dpdk/lib64/librte_gro.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-gso": {
        "archive": "opt/mellanox/dpdk/lib64/librte_gso.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-hash": {
        "archive": "opt/mellanox/dpdk/lib64/librte_hash.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-ip-frag": {
        "archive": "opt/mellanox/dpdk/lib64/librte_ip_frag.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-ipsec": {
        "archive": "opt/mellanox/dpdk/lib64/librte_ipsec.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-jobstats": {
        "archive": "opt/mellanox/dpdk/lib64/librte_jobstats.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-kvargs": {
        "archive": "opt/mellanox/dpdk/lib64/librte_kvargs.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-latencystats": {
        "archive": "opt/mellanox/dpdk/lib64/librte_latencystats.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-log": {
        "archive": "opt/mellanox/dpdk/lib64/librte_log.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-lpm": {
        "archive": "opt/mellanox/dpdk/lib64/librte_lpm.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mbuf": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mbuf.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-member": {
        "archive": "opt/mellanox/dpdk/lib64/librte_member.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mempool": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mempool.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mempool-bucket": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mempool_bucket.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mempool-ring": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mempool_ring.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mempool-stack": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mempool_stack.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-meter": {
        "archive": "opt/mellanox/dpdk/lib64/librte_meter.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-metrics": {
        "archive": "opt/mellanox/dpdk/lib64/librte_metrics.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-mldev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_mldev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-net": {
        "archive": "opt/mellanox/dpdk/lib64/librte_net.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-net-af-packet": {
        "archive": "opt/mellanox/dpdk/lib64/librte_net_af_packet.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-net-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_net_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-net-vhost": {
        "archive": "opt/mellanox/dpdk/lib64/librte_net_vhost.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-net-virtio": {
        "archive": "opt/mellanox/dpdk/lib64/librte_net_virtio.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-node": {
        "archive": "opt/mellanox/dpdk/lib64/librte_node.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pcapng": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pcapng.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pci": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pci.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pdcp": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pdcp.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pdump": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pdump.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pipeline": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pipeline.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-pmu": {
        "archive": "opt/mellanox/dpdk/lib64/librte_pmu.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-port": {
        "archive": "opt/mellanox/dpdk/lib64/librte_port.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-power": {
        "archive": "opt/mellanox/dpdk/lib64/librte_power.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-rawdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_rawdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-rcu": {
        "archive": "opt/mellanox/dpdk/lib64/librte_rcu.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-regex-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_regex_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-regexdev": {
        "archive": "opt/mellanox/dpdk/lib64/librte_regexdev.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-reorder": {
        "archive": "opt/mellanox/dpdk/lib64/librte_reorder.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-rib": {
        "archive": "opt/mellanox/dpdk/lib64/librte_rib.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-ring": {
        "archive": "opt/mellanox/dpdk/lib64/librte_ring.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-sched": {
        "archive": "opt/mellanox/dpdk/lib64/librte_sched.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-security": {
        "archive": "opt/mellanox/dpdk/lib64/librte_security.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-stack": {
        "archive": "opt/mellanox/dpdk/lib64/librte_stack.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-table": {
        "archive": "opt/mellanox/dpdk/lib64/librte_table.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-telemetry": {
        "archive": "opt/mellanox/dpdk/lib64/librte_telemetry.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-timer": {
        "archive": "opt/mellanox/dpdk/lib64/librte_timer.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-vdpa-mlx5": {
        "archive": "opt/mellanox/dpdk/lib64/librte_vdpa_mlx5.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "rte-vhost": {
        "archive": "opt/mellanox/dpdk/lib64/librte_vhost.a",
        "headers": [
            "opt/mellanox/dpdk/include/dpdk"
        ]
    },
    "telemetry": {
        "archive": "opt/mellanox/doca/lib64/libdoca_telemetry.a",
        "headers": [
            "opt/mellanox/doca/include"
        ]
    }
}
