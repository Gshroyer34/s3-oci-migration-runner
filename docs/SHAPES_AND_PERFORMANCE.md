# Worker shapes and performance guidance

For the first portable trial, start with **one VM.Optimized3.Flex worker, 2 OCPUs and 16 GiB RAM**, if capacity is available. The coordinator is E5, 2 OCPUs/16 GB in the foundation template. An E5 worker is a supported alternative when Optimized3 is unavailable. This is a starting configuration for correctness testing, not a throughput commitment.

The installer accepts E4, E5 and Optimized3 x86 workers. Ampere is not supported by this installer even though a separate native OCI benchmark exercised it. Do not choose an ARM image for the supplied container/signing-helper build. Confirm current shape availability and image compatibility in the target region. [Oracle compute shapes](https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm)

## Recorded OCI-only measurements

These are September 17, 2026 Ashburn experiments with both endpoints in OCI Object Storage. They used a separate benchmark harness, not this portable pilot. Historical benchmark summaries were transcribed into this guide; operational campaign artifacts containing tenancy identifiers are intentionally not distributed here.

**Full verification, eight 1 GB objects, three measurements per selected profile:**

| Shape | OCPUs | Concurrent objects | Median useful MB/s | Measured range MB/s |
| --- | --- | --- | --- | --- |
| Optimized3 Flex | 2 | 8 | 195.5 | 193.4–198.5 |
| Standard E5 Flex, AMD | 4 | 4 | 112.4 | 104.1–113.0 |
| Standard A2 Flex, Ampere | 4 | 4 | 109.7 | 108.7–116.0 |

The settings differ by selected profile; this is not an equal-OCPU CPU architecture comparison. Optimized3 used 16 GB RAM, 64 MiB parts and four uploads per object. Do not copy these concurrency settings into the portable runtime and assume equivalent behavior.

**Full verification, the same 32 GB dataset, Optimized3 at 2 OCPUs per worker:**

| Workers | Median useful MB/s | Range MB/s |
| --- | --- | --- |
| 1 | 211.5 | 201.0–215.0 |
| 2 | 358.5 | 329.2–358.7 |
| 4 | 683.8 | 651.8–701.7 |

**Separate copy-only trials:**

| Workload and profile | Workers | Median payload throughput |
| --- | --- | --- |
| 32 × 1 GB; 2 OCPUs, 8 parallel objects | 1 | 553.7 MB/s |
| 32 × 1 GB; 4 OCPUs, 16 parallel objects | 2 | 1.730 GB/s |
| 64 × 10 GB; 4 OCPUs, 16 parallel objects | 2 | 2.732 GB/s, 21.85 Gbps |
| 64 × 10 GB; 4 OCPUs, 16 parallel objects | 4 | 4.390 GB/s, 35.12 Gbps |

Each row summarizes three measured trials. The 640 GB trials used 32 MiB parts and four uploads per object, lasted minutes, and omitted full content rereads. They do not establish indefinite sustained throughput, an AWS-to-OCI rate or a 100 Gbps ceiling. **Copy-only is not a selectable verification mode in this release.**

## Why observed speed differs from network specifications

Network capacity is a ceiling, not a per-application guarantee. The benchmark's full-verification path read source bytes three times and destination bytes once. Useful verified MB/s therefore differs from link traffic. Count only traffic that actually crosses the constrained path when sizing a future interconnect; destination-local verification does not automatically cross AWS–OCI.

Small objects introduce many operations per byte: listing, HEAD, tag reads, property/ACL observations, authentication, transfer setup and verification. A 20 KB-heavy dataset can become request-limited while much bandwidth remains unused. Mixed sizes need separate object-per-second and byte-per-second measurements. More workers help only while the source, destination and ledger can absorb the additional requests.

## Plan a calibration

1. Establish the real route and limiting link; record shared traffic separately.
2. Test representative object sizes with the required verification policy and frozen source.
3. Compare one, two and then four workers. Record useful verified bytes, wall time, requests, retries, CPU, memory and source-path traffic.
4. Tune parallelism within supported bounds before assuming more OCPUs or workers are required.
5. Reserve 15–20% headroom and apply an explicit migration allowance after other traffic. These are proposed operating settings, not enforced autoscaling in this preview.

10 Gbps equals 1.25 GB/s before overhead; 100 Gbps equals 12.5 GB/s. Do not divide a path's rate by useful verified throughput when source rereads also use that path.

Obtain current prices in the target region and account agreement. Historical timed-compute cost per TB excluded idle time, storage, boot volumes, requests and AWS outbound traffic; it is not a deployment quote. No price or account-wide spending cap is embedded in this handoff.
