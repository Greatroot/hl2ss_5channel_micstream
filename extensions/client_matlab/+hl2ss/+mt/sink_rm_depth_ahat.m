
classdef sink_rm_depth_ahat
properties
    host
    port
    chunk
    mode
    divisor
    profile_z
    profile_ab
    level
    bitrate
    options
    buffer_size
    module
end

methods
    function self = sink_rm_depth_ahat(host, port, chunk, mode, divisor, profile_z, profile_ab, level, bitrate, options, buffer_size, module)
        arguments
            host
            port
            chunk       = 4096
            mode        = hl2ss.stream_mode.MODE_1
            divisor     = 1
            profile_z   = hl2ss.depth_profile.SAME
            profile_ab  = hl2ss.video_profile.H265_MAIN
            level       = hl2ss.h26x_level.DEFAULT
            bitrate     = 0
            options     = [hl2ss.h26x_encoder_property.CODECAPI_AVEncMPVGOPSize, 45]
            buffer_size = 450
            module      = @hl2ss_matlab
        end
        
        self.host        = host;
        self.port        = uint16(port);
        self.chunk       = uint64(chunk);
        self.mode        = uint8(mode);
        self.divisor     = uint8(divisor);
        self.profile_z   = uint8(profile_z);
        self.profile_ab  = uint8(profile_ab);
        self.level       = uint8(level);
        self.bitrate     = uint32(bitrate);
        self.options     = uint64(options);
        self.buffer_size = uint64(buffer_size);
        self.module      = module;
    end
    
    function open(self)
        self.module('open', self.host, self.port, self.chunk, self.mode, self.divisor, self.profile_z, self.profile_ab, self.level, self.bitrate, self.options, self.buffer_size);
    end
    
    function response = get_packet_by_index(self, index)
        response = self.module('get_packet', self.port, hl2ss.grab_mode.BY_FRAME_INDEX, int64(index));
    end
    
    function response = get_packet_by_timestamp(self, timestamp, preference)
        response = self.module('get_packet', self.port, hl2ss.grab_mode.BY_TIMESTAMP, uint64(timestamp), int32(preference));
    end
    
    function close(self)
        self.module('close', self.port);
    end
    
    function response = download_calibration(self)
        response = self.module('download_calibration', self.host, self.port);
    end
end
end
